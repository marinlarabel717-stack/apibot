from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.error import BadRequest
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

MENU_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton(BUTTON_PRODUCTS), KeyboardButton(BUTTON_MAIN_MENU)],
        [KeyboardButton(BUTTON_PROFILE), KeyboardButton(BUTTON_RECHARGE)],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


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


async def call_blocking(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


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
            custom_id = None
            if settings.inline_button_custom_emoji_enabled:
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


def main_menu_text(settings: Settings) -> str:
    return (
        f"🏠 {settings.shop_title}\n\n"
        "请选择你要使用的功能：\n"
        "1. 商品列表\n"
        "2. 个人中心\n"
        "3. 我要充值\n\n"
        "也可以直接发送关键字搜索商品。"
    )


def categories_intro() -> str:
    return (
        "🛒 这是商品分类列表，请选择你需要的分类：\n\n"
        "❗首次购买建议先少量测试，确认符合需求再放量。\n"
        "❗虚拟商品一经发货通常不支持无理由处理，请先看清分类与说明。"
    )


def products_intro(category_name: str) -> str:
    return (
        f"🛍 这是商品列表，当前分类：{category_name}\n\n"
        "❗没用过的本店商品，请先少量购买测试，以免造成不必要的争议。\n"
        "❗账号放久难免会死，有差异请联系客服处理。"
    )


def detail_notice() -> str:
    return (
        "❗未使用过的本店商品，请先少量购买测试，以免造成不必要的争议。\n"
        "❗虚拟商品有时效和环境差异，请及时验货。"
    )


def purchase_confirm_caption(product_name: str, unit_price: float, quantity: int) -> str:
    total_price = unit_price * quantity
    return (
        f"🛍 商品：{product_name}\n"
        f"🪙 单价：{format_money(unit_price)} USDT\n"
        f"📦 数量：{quantity}\n\n"
        f"🧾 应付金额：{format_money(total_price)} USDT"
    )


def build_purchase_confirm_keyboard(
    product_id: int,
    quantity: int,
    category_id: int,
    page: int,
) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("✅ 确认购买", callback_data=f"cbuy:{product_id}:{quantity}")],
    ]
    if category_id > 0:
        buttons.append(
            [
                InlineKeyboardButton("🏠 主菜单", callback_data="nav:menu"),
                InlineKeyboardButton("🔙 返回商品", callback_data=f"prd:{product_id}:{category_id}:{page}"),
            ]
        )
    else:
        buttons.append([InlineKeyboardButton("🏠 主菜单", callback_data="nav:menu")])
    return InlineKeyboardMarkup(buttons)


def delivery_ready_caption(
    product_name: str,
    quantity: int,
    quantity_success: int,
    refund_amount: float,
) -> str:
    lines = [
        f"🛍 商品：{product_name}",
        f"📦 数量：{quantity}",
        f"📬 打包完成：库存账号 {quantity_success}",
    ]
    if refund_amount > 0:
        lines.append(f"💸 已退款：{format_money(refund_amount)} USDT")
    return "\n".join(lines)


def delivery_filename(task_id: str, file_url: str) -> str:
    parsed = urlparse(file_url)
    candidate = Path(unquote(parsed.path)).name.strip()
    if not candidate:
        candidate = f"{task_id}.zip"
    if Path(candidate).suffix.lower() != ".zip":
        candidate = f"{Path(candidate).stem or task_id}.zip"
    return candidate


def download_delivery_file(supplier: SupplierClient, task_id: str, file_url: str) -> Path:
    DELIVERY_FILES_DIR.mkdir(parents=True, exist_ok=True)
    target_path = DELIVERY_FILES_DIR / delivery_filename(task_id, file_url)
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


async def reply_inline(update: Update, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    if update.callback_query is not None:
        query = update.callback_query
        await query.answer()
        try:
            await query.edit_message_text(text=text, reply_markup=reply_markup)
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise
    elif update.message is not None:
        await update.message.reply_text(text, reply_markup=reply_markup)


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
        "底部也有常驻按钮：商品列表 / 主菜单 / 个人中心 / 我要充值。"
    )
    await send_menu_message(update, text)


def get_services(context: ContextTypes.DEFAULT_TYPE) -> tuple[Settings, Store, SupplierClient]:
    settings: Settings = context.application.bot_data["settings"]
    store: Store = context.application.bot_data["store"]
    supplier: SupplierClient = context.application.bot_data["supplier"]
    return settings, store, supplier


def build_main_menu_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🛒 商品列表", callback_data="nav:cats")],
            [
                InlineKeyboardButton("👤 个人中心", callback_data="nav:profile"),
                InlineKeyboardButton("💰 我要充值", callback_data="nav:recharge"),
            ],
            [InlineKeyboardButton("📦 我的订单", callback_data="nav:orders")],
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
    buttons.append([InlineKeyboardButton("🏠 主菜单", callback_data="nav:menu")])
    buttons.append([InlineKeyboardButton("❌ 关闭", callback_data="nav:close")])
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
            InlineKeyboardButton("🏠 主菜单", callback_data="nav:menu"),
            InlineKeyboardButton("🔙 返回分类", callback_data="nav:cats"),
        ]
    )
    return InlineKeyboardMarkup(buttons)


def render_products_view(
    category_name: str,
    category_id: int,
    rows: list[dict[str, Any]],
    page: int,
) -> tuple[str, InlineKeyboardMarkup]:
    text = products_intro(category_name)
    keyboard = build_product_keyboard(rows, category_id, page)
    return text, keyboard


def build_product_detail_keyboard(product_id: int, category_id: int, page: int) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = [[InlineKeyboardButton("✅ 购买", callback_data=f"qbuy:{product_id}:1:{category_id}:{page}")]]
    if category_id > 0:
        buttons.append(
            [
                InlineKeyboardButton("🏠 主菜单", callback_data="nav:menu"),
                InlineKeyboardButton("🔙 返回", callback_data=f"cat:{category_id}:{page}"),
            ]
        )
    else:
        buttons.append([InlineKeyboardButton("🏠 主菜单", callback_data="nav:menu")])
    return InlineKeyboardMarkup(buttons)


def render_product_detail_view(
    row: dict[str, Any],
    category_id: int,
    page: int,
) -> tuple[str, InlineKeyboardMarkup]:
    product_id = safe_int(row.get("productId"))
    product_name = str(row.get("productName") or f"商品 {product_id}")
    text = (
        f"✅ 您正在购买：{product_name}\n\n"
        f"📦 商品ID：{product_id}\n"
        f"💰 价格：{safe_float(row.get('price')):.4f} USDT\n"
        f"📊 库存：{safe_int(row.get('totalStock'))}\n\n"
        f"{detail_notice()}\n\n"
        f"手动购买命令：/buy {product_id} 1"
    )
    return text, build_product_detail_keyboard(product_id, category_id, page)


def build_category_keyboard_configured(settings: Settings, rows: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for row in rows:
        category_id = safe_int(row.get("categoryId"))
        stock = safe_int(row.get("totalStock"))
        name = shorten(str(row.get("categoryName") or f"分类 {category_id}"), 26)
        buttons.append([catalog_button(settings, f"{name} 库存 [{stock}]", f"cat:{category_id}:0")])
    buttons.append([InlineKeyboardButton("🏠 主菜单", callback_data="nav:menu")])
    buttons.append([InlineKeyboardButton("❌ 关闭", callback_data="nav:close")])
    return InlineKeyboardMarkup(buttons)


def build_product_keyboard_configured(
    settings: Settings,
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
        stock = safe_int(row.get("totalStock"))
        price = resolve_sell_price(settings, row)
        buttons.append([catalog_button(settings, f"{product_name} 库存 [{stock}] - ${price:.2f}", f"prd:{product_id}:{category_id}:{page}")])

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"cat:{category_id}:{page - 1}"))
    nav_row.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data=f"cat:{category_id}:{page}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("下一页 ➡️", callback_data=f"cat:{category_id}:{page + 1}"))
    buttons.append(nav_row)
    buttons.append(
        [
            InlineKeyboardButton("🏠 主菜单", callback_data="nav:menu"),
            InlineKeyboardButton("🔙 返回分类", callback_data="nav:cats"),
        ]
    )
    return InlineKeyboardMarkup(buttons)


def render_products_view_configured(
    settings: Settings,
    category_name: str,
    category_id: int,
    rows: list[dict[str, Any]],
    page: int,
) -> tuple[str, InlineKeyboardMarkup]:
    return products_intro(category_name), build_product_keyboard_configured(settings, rows, category_id, page)


def render_product_detail_view_configured(
    settings: Settings,
    row: dict[str, Any],
    category_id: int,
    page: int,
) -> tuple[str, InlineKeyboardMarkup]:
    product_id = safe_int(row.get("productId"))
    product_name = str(row.get("productName") or f"商品 {product_id}")
    sell_price = resolve_sell_price(settings, row)
    text = (
        f"✅ 您正在购买：{product_name}\n\n"
        f"📦 商品ID：{product_id}\n"
        f"💰 价格：{sell_price:.4f} USDT\n"
        f"📊 库存：{safe_int(row.get('totalStock'))}\n\n"
        f"{detail_notice()}\n\n"
        f"手动购买命令：/buy {product_id} 1"
    )
    return text, build_product_detail_keyboard(product_id, category_id, page)


async def fetch_categories(supplier: SupplierClient) -> list[dict[str, Any]]:
    payload = await call_blocking(supplier.get_categories)
    return payload.get("data") or []


async def fetch_category_products(supplier: SupplierClient, category_id: int) -> list[dict[str, Any]]:
    payload = await call_blocking(supplier.get_products, category_id)
    return payload.get("data") or []


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, store, _ = get_services(context)
    user = update.effective_user
    if user is not None:
        await call_blocking(store.ensure_user, user.id, user.username or "")
    if update.message is not None:
        await send_menu_message(update, main_menu_text(settings))
        await update.message.reply_text("点击下面按钮开始使用。", reply_markup=build_main_menu_inline())
    elif update.callback_query is not None:
        await reply_inline(update, "点击下面按钮开始使用。", build_main_menu_inline())


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
    await reply_inline(update, categories_intro(), build_category_keyboard_configured(settings, rows))


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
    text, keyboard = render_products_view_configured(settings, category_name, category_id, rows, page)
    await reply_inline(update, text, keyboard)


async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, store, _ = get_services(context)
    user = update.effective_user
    if user is None:
        return
    await call_blocking(store.ensure_user, user.id, user.username or "")
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
            [InlineKeyboardButton("🏠 主菜单", callback_data="nav:menu")],
        ]
    )
    await reply_inline(update, "\n".join(lines), keyboard)


async def show_recharge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, store, _ = get_services(context)
    user = update.effective_user
    balance = 0.0
    if user is not None:
        await call_blocking(store.ensure_user, user.id, user.username or "")
        balance = await call_blocking(store.get_balance, user.id)
    text = (
        f"💰 {settings.shop_title} - 充值中心\n\n"
        f"当前余额：{format_money(balance)} USDT\n\n"
        f"{settings.recharge_text}"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("👤 个人中心", callback_data="nav:profile")],
            [InlineKeyboardButton("🏠 主菜单", callback_data="nav:menu")],
        ]
    )
    await reply_inline(update, text, keyboard)


async def show_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, store, _ = get_services(context)
    user = update.effective_user
    if user is None:
        return
    rows = await call_blocking(store.list_user_orders, user.id, 10)
    if not rows:
        text = "📦 最近订单\n\n你还没有订单记录。"
    else:
        text_lines = ["📦 最近订单", ""]
        for row in rows:
            text_lines.append(
                f"- {row['task_id']} | {row['product_name']} | "
                f"{row['state']} | {row['quantity_success']}/{row['quantity']}"
            )
        text = "\n".join(text_lines)
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("👤 个人中心", callback_data="nav:profile")],
            [InlineKeyboardButton("🏠 主菜单", callback_data="nav:menu")],
        ]
    )
    await reply_inline(update, text, keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_main_menu(update, context)


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_main_menu(update, context)


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
    text, keyboard = render_product_detail_view_configured(settings, row, category_id=0, page=0)
    await update.message.reply_text(text, reply_markup=keyboard)


async def execute_purchase(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    username: str,
    product_id: int,
    quantity: int,
) -> str:
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
    balance = await call_blocking(store.get_balance, user_id)
    return (
        "下单成功，已进入处理中。\n"
        f"订单号: {task_id}\n"
        f"商品: {product_name}\n"
        f"数量: {quantity}\n"
        f"扣款: {format_money(total_price)} USDT\n"
        f"剩余余额: {format_money(balance)} USDT\n\n"
        f"可随时用 /order {task_id} 查状态"
    )


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
    await update.message.reply_text(result, reply_markup=MENU_KEYBOARD)


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
                "订单已完成。",
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
                            await context.bot.send_photo(
                                chat_id=int(final_row["user_id"]),
                                photo=photo_fp,
                                caption=delivery_ready_caption(
                                    str(final_row.get("product_name") or f"商品 {final_row.get('product_id')}"),
                                    quantity,
                                    quantity_success,
                                    refund_amount,
                                ),
                            )
                    with zip_path.open("rb") as document_fp:
                        await context.bot.send_document(
                            chat_id=int(final_row["user_id"]),
                            document=document_fp,
                            filename=zip_path.name,
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
    if text == BUTTON_PRODUCTS:
        await show_categories(update, context)
        return
    if text == BUTTON_MAIN_MENU:
        await show_main_menu(update, context)
        return
    if text == BUTTON_PROFILE:
        await show_profile(update, context)
        return
    if text == BUTTON_RECHARGE:
        await show_recharge(update, context)


async def search_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, _, supplier = get_services(context)
    if update.message is None or not update.message.text:
        return
    keyword = update.message.text.strip()
    if not keyword or keyword in {BUTTON_PRODUCTS, BUTTON_MAIN_MENU, BUTTON_PROFILE, BUTTON_RECHARGE}:
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

    text_lines = [
        f"🔎 搜索结果：{keyword}",
        "点击下面商品按钮查看详情：",
        "",
    ]
    buttons: list[list[InlineKeyboardButton]] = []
    for row in rows[:SEARCH_RESULTS_LIMIT]:
        product_id = safe_int(row.get("productId"))
        category_id = safe_int(row.get("categoryId"))
        text_lines.append(
            f"- {row.get('productName')} | "
            f"库存 {safe_int(row.get('totalStock'))} | "
            f"${safe_float(row.get('price')):.2f}"
        )
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"查看 {shorten(str(row.get('productName')), 22)}",
                    callback_data=f"prd:{product_id}:{category_id}:0",
                )
            ]
        )
    buttons.append([InlineKeyboardButton("🛒 浏览全部分类", callback_data="nav:cats")])
    await update.message.reply_text("\n".join(text_lines), reply_markup=InlineKeyboardMarkup(buttons))


async def search_text_rich(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, _, supplier = get_services(context)
    if update.message is None or not update.message.text:
        return
    keyword = update.message.text.strip()
    if not keyword or keyword in {BUTTON_PRODUCTS, BUTTON_MAIN_MENU, BUTTON_PROFILE, BUTTON_RECHARGE}:
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

    text_lines = [
        f"🔎 搜索结果：{keyword}",
        "点击下面商品按钮查看详情：",
        "",
    ]
    buttons: list[list[InlineKeyboardButton]] = []
    for row in rows[:SEARCH_RESULTS_LIMIT]:
        product_id = safe_int(row.get("productId"))
        category_id = safe_int(row.get("categoryId"))
        sell_price = resolve_sell_price(settings, row)
        text_lines.append(
            f"- {row.get('productName')} | "
            f"库存 {safe_int(row.get('totalStock'))} | "
            f"${sell_price:.2f}"
        )
        buttons.append([catalog_button(settings, f"{shorten(str(row.get('productName')), 22)} | ${sell_price:.2f}", f"prd:{product_id}:{category_id}:0")])
    buttons.append([InlineKeyboardButton("🛒 浏览全部分类", callback_data="nav:cats")])
    await update.message.reply_text("\n".join(text_lines), reply_markup=InlineKeyboardMarkup(buttons))


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, _, supplier = get_services(context)
    query = update.callback_query
    if query is None or not query.data:
        return

    parts = query.data.split(":")
    action = parts[0]

    if action == "nav":
        target = parts[1] if len(parts) > 1 else ""
        if target == "cats":
            await show_categories(update, context)
            return
        if target == "menu":
            await show_main_menu(update, context)
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
        category_id = safe_int(parts[1], -1)
        page = safe_int(parts[2], 0)
        if category_id <= 0:
            await reply_inline(update, "分类参数不合法。")
            return
        await show_products(update, context, category_id, page)
        return

    if action == "prd" and len(parts) == 4:
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
        text, keyboard = render_product_detail_view_configured(settings, row, category_id, page)
        await reply_inline(update, text, keyboard)
        return

    if action == "qbuy" and len(parts) == 5:
        product_id = safe_int(parts[1], -1)
        quantity = safe_int(parts[2], 0)
        category_id = safe_int(parts[3], 0)
        page = safe_int(parts[4], 0)
        if product_id <= 0 or quantity <= 0:
            await reply_inline(update, "快捷购买参数不合法。")
            return
        try:
            payload = await call_blocking(supplier.get_product_detail, product_id)
        except SupplierApiError as exc:
            await reply_inline(update, f"获取商品详情失败: {exc}")
            return
        row = payload.get("data") or {}
        product_name = str(row.get("productName") or f"商品 {product_id}")
        unit_price = resolve_sell_price(settings, row)
        caption = purchase_confirm_caption(product_name, unit_price, quantity)
        keyboard = build_purchase_confirm_keyboard(product_id, quantity, category_id, page)
        await query.answer()
        if query.message is not None and PURCHASE_CONFIRM_IMAGE_PATH.exists():
            with PURCHASE_CONFIRM_IMAGE_PATH.open("rb") as photo_fp:
                await query.message.reply_photo(
                    photo=photo_fp,
                    caption=caption,
                    reply_markup=keyboard,
                )
        elif query.message is not None:
            await query.message.reply_text(caption, reply_markup=keyboard)
        else:
            await reply_inline(update, caption, keyboard)
        return

    if action == "cbuy" and len(parts) == 3:
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
        if query.message is not None:
            await query.message.reply_text(result, reply_markup=MENU_KEYBOARD)
        else:
            await reply_inline(update, result)
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

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", menu))
    application.add_handler(CommandHandler("help", reply_help))
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
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_handler(MessageHandler(filters.Regex(f"^({BUTTON_PRODUCTS}|{BUTTON_MAIN_MENU}|{BUTTON_PROFILE}|{BUTTON_RECHARGE})$"), route_menu_text))
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
