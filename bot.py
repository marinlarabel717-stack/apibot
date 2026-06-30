from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from config import Settings, load_settings
from store import Store
from supplier_client import SupplierApiError, SupplierClient


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("apibot")


def format_money(value: float) -> str:
    return f"{value:.2f}"


def is_admin(settings: Settings, user_id: int) -> bool:
    return int(user_id) in settings.admin_user_ids


async def call_blocking(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


async def reply_help(update: Update, context: ContextTypes.DEFAULT_TYPE | None = None) -> None:
    text = (
        "可用命令:\n"
        "/start - 启动说明\n"
        "/me - 查看我的余额\n"
        "/categories - 查看分类\n"
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, store, _ = get_services(context)
    user = update.effective_user
    if user is None or update.message is None:
        return
    await call_blocking(store.ensure_user, user.id, user.username or "")
    await update.message.reply_text(
        "apibot 已启动。\n"
        "这是一个独立仓库，不跟现有号铺共用代码或数据。\n\n"
        "先用 /categories 看分类，或者直接发关键字搜索商品。"
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
    _, _, supplier = get_services(context)
    if update.message is None:
        return
    try:
        payload = await call_blocking(supplier.get_categories)
    except SupplierApiError as exc:
        await update.message.reply_text(f"获取分类失败: {exc}")
        return
    rows = payload.get("data") or []
    if not rows:
        await update.message.reply_text("当前没有分类。")
        return
    text = ["分类列表:"]
    for row in rows:
        text.append(
            f"- ID {row.get('categoryId')} | {row.get('categoryName')} | 库存 {row.get('totalStock', 0)}"
        )
    text.append("\n使用 /products <category_id> 查看该分类商品")
    await update.message.reply_text("\n".join(text))


async def products(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, _, supplier = get_services(context)
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
    try:
        payload = await call_blocking(supplier.get_products, category_id)
    except SupplierApiError as exc:
        await update.message.reply_text(f"获取商品列表失败: {exc}")
        return
    rows = payload.get("data") or []
    if not rows:
        await update.message.reply_text("这个分类下没有商品。")
        return
    text = [f"分类 {category_id} 商品列表:"]
    for row in rows:
        text.append(
            f"- ID {row.get('productId')} | {row.get('productName')} | 价格 {row.get('price')} | 库存 {row.get('totalStock', 0)}"
        )
    text.append("\n使用 /product <product_id> 查看详情")
    await update.message.reply_text("\n".join(text))


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
        f"商品详情\n"
        f"ID: {row.get('productId')}\n"
        f"名称: {row.get('productName')}\n"
        f"价格: {row.get('price')} USDT\n"
        f"库存: {row.get('totalStock', 0)}\n\n"
        f"购买命令: /buy {row.get('productId')} 1"
    )
    await update.message.reply_text(text)


async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, store, supplier = get_services(context)
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

    await call_blocking(store.ensure_user, user.id, user.username or "")

    try:
        detail_payload = await call_blocking(supplier.get_product_detail, product_id)
    except SupplierApiError as exc:
        await update.message.reply_text(f"获取商品详情失败: {exc}")
        return

    row = detail_payload.get("data") or {}
    unit_price = float(row.get("price") or 0)
    total_stock = int(row.get("totalStock") or 0)
    product_name = str(row.get("productName") or f"商品 {product_id}")
    total_price = unit_price * quantity

    if total_stock < quantity:
        await update.message.reply_text(f"库存不足。当前库存 {total_stock}，你要买 {quantity}")
        return

    ok, remain = await call_blocking(
        store.debit_balance,
        user.id,
        total_price,
        "purchase",
        "",
        f"{product_name} x{quantity}",
    )
    if not ok:
        await update.message.reply_text(
            f"余额不足。\n"
            f"当前余额: {format_money(remain)} USDT\n"
            f"本次需要: {format_money(total_price)} USDT"
        )
        return

    try:
        buy_payload = await call_blocking(supplier.buy_product, product_id, quantity)
    except SupplierApiError as exc:
        refunded = await call_blocking(
            store.add_balance,
            user.id,
            total_price,
            "purchase_refund",
            "",
            f"下单失败退款: {product_name}",
        )
        await update.message.reply_text(
            f"上游下单失败: {exc}\n"
            f"已退款 {format_money(total_price)} USDT\n"
            f"当前余额: {format_money(refunded)} USDT"
        )
        return

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
            user.id,
            total_price,
            "purchase_refund",
            "",
            f"下单失败退款: {product_name}",
        )
        await update.message.reply_text(
            f"下单失败: {upstream_reason}\n"
            f"已退款 {format_money(total_price)} USDT\n"
            f"当前余额: {format_money(refunded)} USDT"
        )
        return

    await call_blocking(
        store.record_order,
        task_id,
        user.id,
        user.username or "",
        product_id,
        product_name,
        quantity,
        unit_price,
        total_price,
        buy_payload,
    )
    balance = await call_blocking(store.get_balance, user.id)
    await update.message.reply_text(
        f"下单成功，已进入处理中。\n"
        f"订单号: {task_id}\n"
        f"商品: {product_name}\n"
        f"数量: {quantity}\n"
        f"扣款: {format_money(total_price)} USDT\n"
        f"剩余余额: {format_money(balance)} USDT\n\n"
        f"可随时用 /order {task_id} 查状态"
    )


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
    status = int(data.get("taskStatus") or 0)
    quantity_success = int(data.get("quantitySuccess") or 0)
    file_url = str(data.get("fileUrl") or "").strip()
    quantity = int(order["quantity"])
    unit_price = float(order["unit_price"])
    total_price = float(order["total_price"])

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
                    f"订单失败，已自动退款。\n"
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
                f"订单已完成。",
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
    state, summary = await finalize_remote_order(context, task_id, notify_user=False)
    local_order = await call_blocking(store.get_order, task_id) or local_order
    lines = [
        f"订单号: {task_id}",
        f"商品: {local_order.get('product_name')}",
        f"状态: {local_order.get('state')}",
        f"数量: {local_order.get('quantity')}",
        f"成功数量: {local_order.get('quantity_success')}",
        f"退款: {format_money(float(local_order.get('refund_amount') or 0))} USDT",
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
            f"- {row['task_id']} | {row['product_name']} | {row['state']} | {row['quantity_success']}/{row['quantity']}"
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
    text = [f"搜索结果: {html.escape(keyword)}"]
    for row in rows[:20]:
        text.append(
            f"- ID {row.get('productId')} | {row.get('productName')} | 价格 {row.get('price')} | 库存 {row.get('totalStock', 0)}"
        )
    text.append("\n用 /product <product_id> 查看详情")
    await update.message.reply_text("\n".join(text), parse_mode=ParseMode.HTML)


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
