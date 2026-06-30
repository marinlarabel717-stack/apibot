from __future__ import annotations

import asyncio
import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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


CATEGORY_BUTTONS_PER_ROW = 2
PRODUCTS_PER_PAGE = 8
SEARCH_RESULTS_LIMIT = 8


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


async def reply_text(
    update: Update,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
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


async def reply_help(update: Update, context: ContextTypes.DEFAULT_TYPE | None = None) -> None:
    text = (
        "可用命令:\n"
        "/start - 启动说明\n"
        "/me - 查看我的余额\n"
        "/categories - 按按钮浏览分类\n"
        "/products <category_id> - 查看某分类商品\n"
        "/product <product_id> - 查看商品详情\n"
        "/buy <product_id> <数量> - 购买商品\n"
        "/orders - 查看最近订单\n"
        "/order <task_id> - 查询订单状态\n"
        "/supplier_balance - 管理员查看上游余额\n"
        "/credit <user_id> <金额> - 管理员加余额\n\n"
        "直接发送文字也可以搜索商品。"
    )
    if update.message:
        await update.message.reply_text(text)


def get_services(context: ContextTypes.DEFAULT_TYPE) -> tuple[Settings, Store, SupplierClient]:
    settings: Settings = context.application.bot_data["settings"]
    store: Store = context.application.bot_data["store"]
    supplier: SupplierClient = context.application.bot_data["supplier"]
    return settings, store, supplier


def build_category_keyboard(rows: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    current_row: list[InlineKeyboardButton] = []
    for row in rows:
        category_id = safe_int(row.get("categoryId"))
        stock = safe_int(row.get("totalStock"))
        name = shorten(str(row.get("categoryName") or f"分类 {category_id}"), 14)
        current_row.append(
            InlineKeyboardButton(
                text=f"ID {category_id} | {name} | 库{stock}",
                callback_data=f"cat:{category_id}:0",
            )
        )
        if len(current_row) == CATEGORY_BUTTONS_PER_ROW:
            buttons.append(current_row)
            current_row = []
    if current_row:
        buttons.append(current_row)
    return InlineKeyboardMarkup(buttons)


def render_categories_view(rows: list[dict[str, Any]]) -> tuple[str, InlineKeyboardMarkup]:
    text_lines = ["分类列表", "点下面按钮直接进入商品列表。"]
    for row in rows:
        text_lines.append(
            f"ID {safe_int(row.get('categoryId'))} | "
            f"{row.get('categoryName')} | 库存 {safe_int(row.get('totalStock'))}"
        )
    return "\n".join(text_lines), build_category_keyboard(rows)


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
        name = shorten(str(row.get("productName") or f"商品 {product_id}"), 24)
        stock = safe_int(row.get("totalStock"))
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"ID {product_id} | {name} | 库{stock}",
                    callback_data=f"prd:{product_id}:{category_id}:{page}",
                )
            ]
        )

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton(
                text="上一页",
                callback_data=f"cat:{category_id}:{page - 1}",
            )
        )
    nav_row.append(
        InlineKeyboardButton(
            text=f"{page + 1}/{total_pages}",
            callback_data=f"cat:{category_id}:{page}",
        )
    )
    if page < total_pages - 1:
        nav_row.append(
            InlineKeyboardButton(
                text="下一页",
                callback_data=f"cat:{category_id}:{page + 1}",
            )
        )
    buttons.append(nav_row)
    buttons.append([InlineKeyboardButton(text="返回分类", callback_data="nav:cats")])
    return InlineKeyboardMarkup(buttons)


def render_products_view(
    category_id: int,
    rows: list[dict[str, Any]],
    page: int,
) -> tuple[str, InlineKeyboardMarkup]:
    total_pages = max(1, (len(rows) + PRODUCTS_PER_PAGE - 1) // PRODUCTS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * PRODUCTS_PER_PAGE
    page_rows = rows[start : start + PRODUCTS_PER_PAGE]

    text_lines = [
        f"分类 {category_id} 商品列表",
        f"第 {page + 1}/{total_pages} 页，点按钮看详情。",
        "",
    ]
    for row in page_rows:
        text_lines.append(
            f"ID {safe_int(row.get('productId'))} | "
            f"{row.get('productName')} | "
            f"价格 {safe_float(row.get('price')):.4f} | "
            f"库存 {safe_int(row.get('totalStock'))}"
        )
    return "\n".join(text_lines), build_product_keyboard(rows, category_id, page)


def build_product_detail_keyboard(product_id: int, category_id: int, page: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="购买 1 个",
                callback_data=f"qbuy:{product_id}:1",
            ),
            InlineKeyboardButton(
                text="购买 5 个",
                callback_data=f"qbuy:{product_id}:5",
            ),
        ]
    ]
    if category_id > 0:
        rows.append([InlineKeyboardButton(text="返回商品列表", callback_data=f"cat:{category_id}:{page}")])
    rows.append([InlineKeyboardButton(text="返回分类", callback_data="nav:cats")])
    return InlineKeyboardMarkup(rows)


def render_product_detail_view(
    row: dict[str, Any],
    category_id: int,
    page: int,
) -> tuple[str, InlineKeyboardMarkup]:
    product_id = safe_int(row.get("productId"))
    text = (
        "商品详情\n"
        f"ID: {product_id}\n"
        f"名称: {row.get('productName')}\n"
        f"价格: {safe_float(row.get('price')):.4f} USDT\n"
        f"库存: {safe_int(row.get('totalStock'))}\n\n"
        f"也可以手动输入: /buy {product_id} 1"
    )
    return text, build_product_detail_keyboard(product_id, category_id, page)


async def fetch_categories(supplier: SupplierClient) -> list[dict[str, Any]]:
    payload = await call_blocking(supplier.get_categories)
    return payload.get("data") or []


async def fetch_category_products(supplier: SupplierClient, category_id: int) -> list[dict[str, Any]]:
    payload = await call_blocking(supplier.get_products, category_id)
    return payload.get("data") or []


async def show_categories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, _, supplier = get_services(context)
    try:
        rows = await fetch_categories(supplier)
    except SupplierApiError as exc:
        await reply_text(update, f"获取分类失败: {exc}")
        return
    if not rows:
        await reply_text(update, "当前没有分类。")
        return
    text, keyboard = render_categories_view(rows)
    await reply_text(update, text, keyboard)


async def show_products(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    category_id: int,
    page: int = 0,
) -> None:
    _, _, supplier = get_services(context)
    try:
        rows = await fetch_category_products(supplier, category_id)
    except SupplierApiError as exc:
        await reply_text(update, f"获取商品列表失败: {exc}")
        return
    if not rows:
        await reply_text(update, "这个分类下没有商品。")
        return
    text, keyboard = render_products_view(category_id, rows, page)
    await reply_text(update, text, keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, store, _ = get_services(context)
    user = update.effective_user
    if user is None or update.message is None:
        return
    await call_blocking(store.ensure_user, user.id, user.username or "")
    await update.message.reply_text(
        "apibot 已启动。\n"
        "这是一个独立仓库，不跟现有号铺共用代码或数据。\n\n"
        "先用 /categories 点按钮浏览分类，或者直接发关键字搜索商品。"
    )
    await reply_help(update)


async def me(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, store, _ = get_services(context)
    user = update.effective_user
    if user is None or update.message is None:
        return
    await call_blocking(store.ensure_user, user.id, user.username or "")
    balance = await call_blocking(store.get_balance, user.id)
    await update.message.reply_text(f"你的余额: {format_money(balance)} USDT")


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
    _, _, supplier = get_services(context)
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
    text = (
        "商品详情\n"
        f"ID: {safe_int(row.get('productId'))}\n"
        f"名称: {row.get('productName')}\n"
        f"价格: {safe_float(row.get('price')):.4f} USDT\n"
        f"库存: {safe_int(row.get('totalStock'))}\n\n"
        f"购买命令: /buy {safe_int(row.get('productId'))} 1"
    )
    await update.message.reply_text(text)


async def execute_purchase(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    username: str,
    product_id: int,
    quantity: int,
) -> str:
    _, store, supplier = get_services(context)
    await call_blocking(store.ensure_user, user_id, username)

    detail_payload = await call_blocking(supplier.get_product_detail, product_id)
    row = detail_payload.get("data") or {}
    unit_price = safe_float(row.get("price"))
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
    await update.message.reply_text(result)


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
            if file_url:
                lines.append(f"下载地址: {file_url}")
            await context.bot.send_message(chat_id=int(final_row["user_id"]), text="\n".join(lines))
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
        lines.append(f"下载地址: {local_order['file_url']}")
    await update.message.reply_text("\n".join(lines))


async def orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, store, _ = get_services(context)
    user = update.effective_user
    if user is None or update.message is None:
        return
    rows = await call_blocking(store.list_user_orders, user.id, 10)
    if not rows:
        await update.message.reply_text("你还没有订单。")
        return
    text = ["最近订单:"]
    for row in rows:
        text.append(
            f"- {row['task_id']} | {row['product_name']} | {row['state']} | "
            f"{row['quantity_success']}/{row['quantity']}"
        )
    await update.message.reply_text("\n".join(text))


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
        f"accountBalance: {data.get('accountBalance')}"
    )


async def credit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, store, _ = get_services(context)
    user = update.effective_user
    if user is None or update.message is None:
        return
    if not is_admin(settings, user.id):
        await update.message.reply_text("只有管理员可以加余额。")
        return
    if len(context.args) < 2:
        await update.message.reply_text("用法: /credit <user_id> <金额>")
        return
    try:
        target_user_id = int(context.args[0])
        amount = float(context.args[1])
    except ValueError:
        await update.message.reply_text("user_id 或 金额 格式不对")
        return
    if amount <= 0:
        await update.message.reply_text("金额必须大于 0")
        return
    balance = await call_blocking(store.add_balance, target_user_id, amount, "admin_credit", "", f"by {user.id}")
    await update.message.reply_text(
        f"已给用户 {target_user_id} 加 {format_money(amount)} USDT\n"
        f"当前余额: {format_money(balance)} USDT"
    )


async def search_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, _, supplier = get_services(context)
    if update.message is None or not update.message.text:
        return
    keyword = update.message.text.strip()
    if not keyword:
        return
    try:
        payload = await call_blocking(supplier.search_products, keyword)
    except SupplierApiError as exc:
        await update.message.reply_text(f"搜索失败: {exc}")
        return
    rows = payload.get("data") or []
    if not rows:
        await update.message.reply_text("没有搜到商品。")
        return

    text_lines = [f"搜索结果: {keyword}", "先给你前几个匹配商品：", ""]
    buttons: list[list[InlineKeyboardButton]] = []
    for row in rows[:SEARCH_RESULTS_LIMIT]:
        product_id = safe_int(row.get("productId"))
        category_id = safe_int(row.get("categoryId"))
        text_lines.append(
            f"ID {product_id} | {row.get('productName')} | "
            f"价格 {safe_float(row.get('price')):.4f} | 库存 {safe_int(row.get('totalStock'))}"
        )
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"查看 ID {product_id}",
                    callback_data=f"prd:{product_id}:{category_id}:0",
                )
            ]
        )
    buttons.append([InlineKeyboardButton(text="浏览全部分类", callback_data="nav:cats")])
    await update.message.reply_text(
        "\n".join(text_lines),
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, _, supplier = get_services(context)
    query = update.callback_query
    if query is None or not query.data:
        return

    parts = query.data.split(":")
    action = parts[0]

    if action == "nav" and len(parts) == 2 and parts[1] == "cats":
        await show_categories(update, context)
        return

    if action == "cat" and len(parts) == 3:
        category_id = safe_int(parts[1], -1)
        page = safe_int(parts[2], 0)
        if category_id <= 0:
            await reply_text(update, "分类参数不合法。")
            return
        await show_products(update, context, category_id, page)
        return

    if action == "prd" and len(parts) == 4:
        product_id = safe_int(parts[1], -1)
        category_id = safe_int(parts[2], 0)
        page = safe_int(parts[3], 0)
        if product_id <= 0:
            await reply_text(update, "商品参数不合法。")
            return
        try:
            payload = await call_blocking(supplier.get_product_detail, product_id)
        except SupplierApiError as exc:
            await reply_text(update, f"获取商品详情失败: {exc}")
            return
        row = payload.get("data") or {}
        text, keyboard = render_product_detail_view(row, category_id, page)
        await reply_text(update, text, keyboard)
        return

    if action == "qbuy" and len(parts) == 3:
        user = update.effective_user
        product_id = safe_int(parts[1], -1)
        quantity = safe_int(parts[2], 0)
        if user is None or product_id <= 0 or quantity <= 0:
            await reply_text(update, "快捷购买参数不合法。")
            return
        try:
            result = await execute_purchase(context, user.id, user.username or "", product_id, quantity)
        except SupplierApiError as exc:
            await reply_text(update, f"获取商品详情失败: {exc}")
            return
        await reply_text(update, result)
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
    application.add_handler(CommandHandler("help", reply_help))
    application.add_handler(CommandHandler("me", me))
    application.add_handler(CommandHandler("categories", categories))
    application.add_handler(CommandHandler("products", products))
    application.add_handler(CommandHandler("product", product))
    application.add_handler(CommandHandler("buy", buy))
    application.add_handler(CommandHandler("orders", orders))
    application.add_handler(CommandHandler("order", order))
    application.add_handler(CommandHandler("supplier_balance", supplier_balance))
    application.add_handler(CommandHandler("credit", credit))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search_text))

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
