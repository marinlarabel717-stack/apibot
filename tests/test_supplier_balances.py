import asyncio
import gc
import unittest
import sqlite3
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import bot
from supplier_client import DaxiSupplierClient, SupplierApiError
from tgstorex_client import TgstorexSupplierClient
from store import Store
from test_account_lists import SETTINGS, make_context as base_context, make_update


def make_context():
    context = base_context()
    context.application.bot_data["store"].get_supplier_sales_stats.return_value = {}
    return context


class SupplierBalanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_entry_and_callback_use_balance_panel(self):
        buttons = [b for row in bot.build_admin_home_keyboard().inline_keyboard for b in row]
        self.assertTrue(any(b.text == "查看余额" and b.callback_data == "adm:balances" for b in buttons))
        update, context = make_update(callback="adm:balances"), make_context()
        with patch.object(bot, "show_admin_supplier_balances", AsyncMock()) as show:
            await bot.handle_admin_callback(update, context, ["adm", "balances"])
            show.assert_awaited_once_with(update, context)

    async def test_all_sources_queried_concurrently_with_notes_even_when_closed(self):
        context, update = make_context(), make_update()
        started = set()

        async def query(index):
            started.add(index)
            for _ in range(20):
                if len(started) == 4:
                    break
                await asyncio.sleep(0)
            self.assertEqual(len(started), 4)
            return {"success": True, "data": {"accountBalance": index, "userName": "upstream-user"}}

        clients = [MagicMock(query_balance=AsyncMock(side_effect=lambda: None)) for _ in range(4)]
        for i, client in enumerate(clients):
            async def run(index=i):
                return await query(index)
            client.query_balance = run
        targets = list(zip(("列表①", "列表②", "列表③", "列表④"), clients))
        for source in (bot.LEGACY_SOURCE, bot.DAXI_SOURCE, bot.RZ_SOURCE):
            context.application.bot_data["runtime_config"][bot.account_list_runtime_key(source)] = "0"
        async def call(func):
            result = func()
            return await result if asyncio.iscoroutine(result) else result
        with patch.object(bot, "get_supplier_balance_targets", return_value=targets), \
             patch.object(bot, "call_blocking", side_effect=call), \
             patch.object(bot, "reply_inline", AsyncMock()) as reply:
            await bot.show_admin_supplier_balances(update, context)
        text = reply.await_args.args[1]
        for number, note in zip("①②③④", ("旧接口", "DAXI", "RZ / DariMarket", "Tgstorex · 会话包")):
            self.assertIn("账号列表" + number, text)
            self.assertIn("备注：" + note, text)
        self.assertIn("0.00 U", text)
        self.assertIn("3.00 USD", text)
        self.assertIn("上游账号：upstream-user", text)
        self.assertEqual(reply.await_count, 2)

    async def test_failure_timeout_and_unconfigured_are_not_zero(self):
        good = MagicMock(query_balance=MagicMock(return_value={"data": {"accountBalance": "12.50"}}))
        failed = MagicMock(query_balance=MagicMock(side_effect=SupplierApiError("secret-token")))
        slow = MagicMock(query_balance=MagicMock(side_effect=asyncio.TimeoutError()))
        with patch.object(bot, "get_supplier_balance_targets", return_value=[("列表①", good), ("列表②", failed), ("列表④", slow)]), \
             patch.object(bot, "reply_inline", AsyncMock()) as reply:
            await bot.show_admin_supplier_balances(make_update(), make_context())
        text = reply.await_args.args[1]
        self.assertIn("12.50 U", text)
        for status in ("查询失败", "查询超时", "未配置"):
            self.assertIn(status, text)
        self.assertNotIn("可用余额：0.00", text)
        self.assertNotIn("secret-token", text)

    async def test_missing_and_nonfinite_balances_never_display_as_zero(self):
        for value in (None, "NaN", "Infinity", True, "not-money"):
            client = MagicMock(query_balance=MagicMock(return_value={"data": {"accountBalance": value}}))
            with self.subTest(value=value), \
                 patch.object(bot, "get_supplier_balance_targets", return_value=[("列表①", client)]), \
                 patch.object(bot, "reply_inline", AsyncMock()) as reply:
                await bot.show_admin_supplier_balances(make_update(), make_context())
                self.assertIn("查询失败", reply.await_args.args[1])
                self.assertNotIn("可用余额：0.00", reply.await_args.args[1])

    async def test_sales_and_missing_costs_shown_even_without_supplier_configuration(self):
        context = make_context()
        context.application.bot_data["store"].get_supplier_sales_stats.return_value = {
            "legacy": {"sold_quantity": 16, "sold_orders": 3, "net_sales": 48,
                       "known_profit": 12, "missing_cost_orders": 1},
        }
        with patch.object(bot, "get_supplier_balance_targets", return_value=[]), \
             patch.object(bot, "reply_inline", AsyncMock()) as reply:
            await bot.show_admin_supplier_balances(make_update(), context)
        text = reply.await_args.args[1]
        self.assertIn("累计售出：16 个 / 3 单", text)
        self.assertIn("实收销售额：48.00 U", text)
        self.assertIn("商品利润（已记录成本）：12.00 U", text)
        self.assertIn("成本缺失：1 单，未计入利润", text)

    async def test_database_failure_does_not_hide_supplier_balances(self):
        context = make_context()
        context.application.bot_data["store"].get_supplier_sales_stats.side_effect = sqlite3.OperationalError()
        client = MagicMock(query_balance=MagicMock(return_value={"data": {"accountBalance": 5}}))
        with patch.object(bot, "get_supplier_balance_targets", return_value=[("列表①", client)]), \
             patch.object(bot, "reply_inline", AsyncMock()) as reply:
            await bot.show_admin_supplier_balances(make_update(), context)
        self.assertIn("可用余额：5.00 U", reply.await_args.args[1])
        self.assertIn("销售统计：暂不可用", reply.await_args.args[1])

    async def test_non_admin_cannot_query_or_open_callback(self):
        update, context = make_update(callback="adm:balances"), make_context()
        update.effective_user.id = 99
        with patch.object(bot, "get_supplier_balance_targets") as targets, \
             patch.object(bot, "show_admin_supplier_balances", AsyncMock()) as show:
            await bot.handle_admin_callback(update, context, ["adm", "balances"])
            show.assert_not_awaited()
            targets.assert_not_called()
        with patch.object(bot, "get_supplier_balance_targets") as targets:
            await bot.show_admin_supplier_balances(update, context)
            targets.assert_not_called()

    async def test_configured_targets_include_four_and_ignore_sales_switches(self):
        context = make_context()
        context.application.bot_data["settings"] = replace(SETTINGS, daxi_api_key="test", rz_api_key="test")
        self.assertEqual([name for name, _ in bot.get_supplier_balance_targets(context)], ["列表①", "列表②", "列表③", "列表④"])


class BalanceAdapterTests(unittest.TestCase):
    def test_tgstorex_reads_wallet_from_read_only_order_metadata(self):
        client = TgstorexSupplierClient(SETTINGS)
        for amount in (0, "19.75"):
            with patch.object(client, "_request_json", return_value={"data": [], "meta": {"wallet_balance": amount}}) as request:
                self.assertEqual(client.query_balance()["data"]["accountBalance"], float(amount))
                request.assert_called_once_with("GET", "/orders", params={"order_type": "sessions", "per_page": 1})
        for meta in ({}, {"wallet_balance": None}, {"wallet_balance": "NaN"}, {"wallet_balance": True}):
            with patch.object(client, "_request_json", return_value={"data": [], "meta": meta}):
                with self.assertRaises(SupplierApiError):
                    client.query_balance()

    def test_daxi_zero_balance_not_overwritten_by_fallback(self):
        client = DaxiSupplierClient(SETTINGS)
        with patch.object(DaxiSupplierClient, "_request_json", return_value={"data": {"accountBalance": 0, "balance": 99}}):
            self.assertEqual(client.query_balance()["data"]["accountBalance"], 0)


class SalesStatsTests(unittest.TestCase):
    def test_source_totals_partial_refunds_pending_and_missing_historical_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "orders.db")
            store.ensure_user(1)
            self.assertEqual(store.get_supplier_sales_stats(), {})
            for source in ("legacy", "daxi", "rz", "tgstorex"):
                for suffix, qty, success, state, cost in (
                    ("full", 10, 10, "completed", 2),
                    ("partial", 5, 2, "partial", 2),
                    ("historical", 4, 4, "completed", None),
                    ("failed", 5, 0, "failed", 2),
                    ("pending", 100, 0, "processing", 2),
                ):
                    task_id = source + suffix
                    store.record_order(task_id, 1, "test", "DE", "test", qty, 3, qty * 3, {}, source, unit_cost=cost)
                    if state != "processing":
                        refund = (qty - success) * 3
                        store.finalize_order(task_id, state, success, "", refund, {})
                        store.finalize_order(task_id, state, success, "", refund, {})
                stats = store.get_supplier_sales_stats()[source]
                self.assertEqual(stats["sold_quantity"], 16)
                self.assertEqual(stats["sold_orders"], 3)
                self.assertEqual(stats["net_sales"], 48)
                self.assertEqual(stats["known_profit"], 12)
                self.assertEqual(stats["missing_cost_orders"], 1)
            reopened = Store(store.db_path)
            self.assertEqual(reopened.get_supplier_sales_stats(), store.get_supplier_sales_stats())
            gc.collect()

    def test_old_database_migrates_without_fabricating_historical_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.db"
            with sqlite3.connect(path) as conn:
                conn.execute("""CREATE TABLE orders (
                    task_id TEXT PRIMARY KEY, user_id INTEGER, username TEXT, product_id INTEGER,
                    product_name TEXT, quantity INTEGER, quantity_success INTEGER,
                    unit_price REAL, total_price REAL, refund_amount REAL, state TEXT,
                    file_url TEXT, raw_payload TEXT, created_at TEXT, updated_at TEXT)""")
                conn.execute("INSERT INTO orders VALUES ('old', 1, '', 1, 'old', 2, 2, 3, 6, 0, 'completed', '', '', '', '')")
            conn.close()
            store = Store(path)
            order = store.get_order("old")
            self.assertIsNone(order["unit_cost"])
            stats = store.get_supplier_sales_stats()["legacy"]
            self.assertEqual(stats["sold_quantity"], 2)
            self.assertEqual(stats["net_sales"], 6)
            self.assertEqual(stats["known_profit"], 0)
            self.assertEqual(stats["missing_cost_orders"], 1)
            gc.collect()
