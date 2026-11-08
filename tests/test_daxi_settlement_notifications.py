import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from test_account_lists import make_context
import bot
from store import Store
from supplier_client import DaxiSupplierClient


class ClosingStore(Store):
    @contextmanager
    def _connect(self):
        with closing(super()._connect()) as connection:
            with connection:
                yield connection


class DaxiSettlementNotificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ClosingStore(Path(self.temp.name) / "orders.db")
        self.store.ensure_user(2, "buyer", "猴王")
        self.store.add_balance(2, 25.05, "test")
        self.store.debit_balance(2, 10.2, "purchase")
        self.store.record_order("order-test", 2, "buyer", "product", "+61 澳大利亚",
                                6, 1.7, 10.2, {}, bot.DAXI_SOURCE, unit_cost=1.6)
        self.context = make_context(self.store)
        self.supplier = MagicMock(spec=DaxiSupplierClient)
        self.context.application.bot_data["supplier_daxi"] = self.supplier

    async def settle(self, delivered=2, status="partial", upstream_refund="2.05", notify_user=False):
        self.supplier.query_order.return_value = {"data": {
            "status": status, "quantityDelivered": delivered, "amountRefunded": upstream_refund,
        }}
        return await bot.finalize_remote_order(self.context, "order-test", notify_user=notify_user)

    def admin_messages(self):
        return [call.kwargs for call in self.context.bot.send_message.await_args_list
                if call.kwargs["chat_id"] == 1]

    async def test_partial_delivery_uses_retail_refund_and_settled_balance(self):
        result = await self.settle()
        self.assertEqual(result[0], "partial")
        message, = self.admin_messages()
        text = message["text"]
        for expected in ("用户: 猴王 @buyer", "购买来源：账号列表②", "商品单价：1.70\n",
                         "购买数量：6", "实发数量：2", "退款余额：6.80\n",
                         "扣除金额：3.40", "剩余余额：21.65"):
            self.assertIn(expected, text)
        self.assertNotIn("USDT", text)
        self.assertAlmostEqual(self.store.get_user(2)["balance"], 21.65)
        self.assertAlmostEqual(self.store.get_order("order-test")["refund_amount"], 6.8)
        encoded = text.encode("utf-16-le")
        for emoji_id, emoji in (("5926783847453692661", "🛡"), ("5960551395730919906", "📝"),
                                ("5845943483382110702", "🔄")):
            entity, = [entity for entity in message["entities"] if entity.custom_emoji_id == emoji_id]
            self.assertEqual(encoded[entity.offset * 2:(entity.offset + entity.length) * 2].decode("utf-16-le"), emoji)
        user_id, = [entity for entity in message["entities"] if entity.type == "code"]
        self.assertEqual(encoded[user_id.offset * 2:(user_id.offset + user_id.length) * 2].decode("utf-16-le"), "2")

    async def test_one_missing_account_does_not_refund_upstream_cost(self):
        await self.settle(delivered=5)
        message, = self.admin_messages()
        self.assertIn("退款余额：1.70\n", message["text"])
        self.assertIn("扣除金额：8.50", message["text"])
        self.assertIn("剩余余额：16.55", message["text"])

    async def test_completed_order_has_zero_refund_even_if_upstream_reports_refund(self):
        await self.settle(delivered=6, status="completed", upstream_refund="99")
        message, = self.admin_messages()
        self.assertIn("实发数量：6", message["text"])
        self.assertIn("退款余额：0.00\n", message["text"])
        self.assertIn("扣除金额：10.20", message["text"])
        self.assertIn("剩余余额：14.85", message["text"])

    async def test_zero_delivery_refunds_full_retail_payment(self):
        await self.settle(delivered=0)
        message, = self.admin_messages()
        self.assertIn("实发数量：0", message["text"])
        self.assertIn("退款余额：10.20\n", message["text"])
        self.assertIn("扣除金额：0.00", message["text"])
        self.assertAlmostEqual(self.store.get_user(2)["balance"], 25.05)

    async def test_failed_order_notifies_and_refunds_once(self):
        await self.settle(status="failed")
        await self.settle(status="failed")
        message, = self.admin_messages()
        self.assertIn("实发数量：0", message["text"])
        self.assertIn("退款余额：10.20\n", message["text"])
        self.assertAlmostEqual(self.store.get_user(2)["balance"], 25.05)

    async def test_repeat_queries_do_not_refund_or_notify_again(self):
        await self.settle()
        await self.settle()
        self.assertEqual(len(self.admin_messages()), 1)
        self.assertAlmostEqual(self.store.get_user(2)["balance"], 21.65)

    async def test_processing_waits_for_final_delivery_quantity(self):
        await self.settle(status="processing")
        self.assertEqual(self.admin_messages(), [])
        self.assertEqual(self.store.get_order("order-test")["state"], "processing")

    async def test_invalid_delivery_count_does_not_settle_or_refund(self):
        for delivered in (-1, 7, None, True, "not-a-number", 2.5):
            with self.subTest(delivered=delivered):
                self.assertEqual((await self.settle(delivered=delivered))[0], "error")
                self.assertEqual(self.store.get_order("order-test")["state"], "processing")
                self.assertEqual(self.admin_messages(), [])

    async def test_user_notification_and_delivery_are_preserved(self):
        self.supplier.query_order.return_value = {"data": {
            "status": "partial", "quantityDelivered": 2,
            "files": [{"downloadUrl": "https://example.test/accounts.zip"}],
        }}
        with patch.object(bot, "deliver_order_file", AsyncMock()) as deliver:
            await bot.finalize_remote_order(self.context, "order-test", notify_user=True)
        deliver.assert_awaited_once()
        self.assertEqual(len(self.admin_messages()), 1)
        user_message, = [call.kwargs for call in self.context.bot.send_message.await_args_list
                         if call.kwargs["chat_id"] == 2]
        self.assertIn("6.80", user_message["text"])

    async def test_other_sources_do_not_receive_extra_settlement_notification(self):
        for source in (bot.LEGACY_SOURCE, bot.RZ_SOURCE, bot.TGSTOREX_SOURCE):
            await bot.notify_admin_daxi_settlement(self.context, {"source": source})
        self.assertEqual(self.admin_messages(), [])
