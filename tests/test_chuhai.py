import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch
from urllib.parse import urlencode, unquote_plus

import bot
from chuhai_client import ChuhaiSupplierClient
from supplier_client import SupplierApiError
from test_account_lists import SETTINGS, make_context, make_update


class SharedApiTests(unittest.TestCase):
    def client(self):
        return ChuhaiSupplierClient(replace(SETTINGS, chuhai_app_id="123", chuhai_app_key="secret+key", chuhai_cny_per_usdt=0, chuhai_price_rate=0.14))

    def test_signature_and_parameter_case(self):
        client = self.client()
        client.session = MagicMock()
        client.session.post.return_value.json.return_value = {"code": 200, "data": {}}
        client._request("/shared/commodity/query2", requestNo="a+b & c", unused="")
        args = client.session.post.call_args
        data = args.kwargs["data"]
        expected = hashlib.md5(unquote_plus(urlencode({"app_id": "123", "requestNo": "a+b & c"}) + "&key=secret+key").encode()).hexdigest()
        self.assertEqual(data, {"app_id": "123", "requestNo": "a+b & c", "sign": expected})
        self.assertFalse(args.kwargs["allow_redirects"])

    def test_catalog_specs_cost_inventory_and_callback_id(self):
        client = self.client()
        catalog = [{"name": "Accounts", "children": [{"code": "long:code", "name": "Account", "delivery_way": 0, "config": {"category_factory": {"US:a": "10"}}}]}]
        inventory = {"count": 3, "delivery_way": 0, "draft_status": 0, "config": {"category_factory": {"US:a": "12"}}}
        with patch.object(client, "_request", side_effect=[catalog, inventory]):
            row = client.get_product_detail(hashlib.sha256(b"long:code\0US:a").hexdigest()[:20])["data"]
        self.assertEqual(row["totalStock"], 3)
        self.assertAlmostEqual(row["price"], 1.68)
        self.assertNotIn(":", row["productId"])

    def test_ini_specs_and_full_catalog_without_inventory_fanout(self):
        client = self.client()
        data = [{"name": "All", "children": [
            {"code": "spec", "name": "Variants", "delivery_way": 0, "factory_price": 2,
             "config": "[category_factory]\nUS=14\nuk=21\n"},
            {"code": "manual", "name": "Manual", "delivery_way": 1, "factory_price": 3},
            {"code": "widget", "name": "Custom", "delivery_way": 0, "factory_price": 4,
             "widget": '[{"name":"url"}]'},
            {"code": "draft", "name": "Preselected", "delivery_way": 0, "factory_price": 5, "draft_status": 1},
        ]}]
        with patch.object(client, "_request", return_value=data) as request:
            rows = client.get_products()["data"]
        request.assert_called_once_with("/shared/commodity/items")
        self.assertEqual(len(rows), 5)
        self.assertEqual({row["race"] for row in rows if row["shared_code"] == "spec"}, {"US", "uk"})
        self.assertEqual({row["shared_code"] for row in rows}, {"spec", "manual", "widget", "draft"})
        self.assertTrue(all(row["purchaseBlocked"] for row in rows if row["shared_code"] != "spec"))

    def test_visible_inventory_is_bounded_cached_and_failure_is_unknown(self):
        client = self.client()
        client._catalog = {str(index): {"productId": str(index), "shared_code": str(index), "race": "", "stockUnknown": True} for index in range(200)}
        with patch.object(client, "_request", return_value={"count": 42}) as request:
            rows = client.refresh_inventory(list(client._catalog))
            self.assertEqual(request.call_count, 30)
            self.assertTrue(all(row["totalStock"] == 42 and not row["stockUnknown"] for row in rows))
            client.refresh_inventory(["0", "1"])
            self.assertEqual(request.call_count, 30)
        with patch.object(client, "_request", side_effect=SupplierApiError("unavailable")):
            row = client.refresh_inventory(["100"])[0]
        self.assertTrue(row["stockUnknown"])

    def test_rmb_cost_is_divided_before_markup(self):
        settings = replace(SETTINGS, chuhai_cny_per_usdt=8, chuhai_price_rate=0, sell_price_mode="fixed_add", sell_price_value=0.5)
        client = ChuhaiSupplierClient(settings)
        self.assertAlmostEqual(16 * client._rate(), 2)
        self.assertAlmostEqual(bot.resolve_sell_price(settings, {"price": 16 * client._rate()}), 2.5)
        with patch.object(client, "_request", return_value={"balance": 80}):
            data = client.query_balance()["data"]
        self.assertAlmostEqual(data["accountBalance"], 10)
        self.assertEqual(data["currency"], "USDT")
        self.assertEqual(data["sourceBalance"], 80)
        self.assertEqual(data["sourceCurrency"], "CNY")

    def test_invalid_currency_rate_blocks_sale(self):
        client = ChuhaiSupplierClient(replace(SETTINGS, chuhai_cny_per_usdt=0, chuhai_price_rate=0))
        with self.assertRaises(SupplierApiError):
            client.get_products()

    def test_ambiguous_purchase_keeps_recoverable_request(self):
        client = self.client()
        with patch.object(client, "get_product_detail", return_value={"data": {"shared_code": "code", "race": ""}}), patch.object(client, "_request", side_effect=[{}, SupplierApiError("timeout")]) as request:
            result = client.buy_product("p", 2, "local-order")["data"]
        task = result["orderId"]
        self.assertTrue(task.startswith("ch5-"))
        self.assertTrue(task.endswith("-2"))
        self.assertEqual(request.call_count, 2)
        with patch.object(client, "_request", return_value={"secret": "account1\naccount2"}) as query:
            order = client.query_order(task)["data"]
        self.assertEqual(query.call_args.kwargs["requestNo"], task.split("-")[1])
        self.assertEqual(order["quantityDelivered"], 2)
        self.assertEqual(order["status"], "completed")
        with tempfile.TemporaryDirectory() as directory, patch.object(client, "query_order", return_value={"data": order}):
            target = client.download_secret(order["files"][0]["downloadUrl"], Path(directory) / "delivery.txt")
            self.assertEqual(target.read_text(encoding="utf-8"), "account1\naccount2")

    def test_definitive_rejection_is_refundable(self):
        client = self.client()
        with patch.object(client, "get_product_detail", return_value={"data": {"shared_code": "code", "race": ""}}), patch.object(client, "_request", side_effect=[{}, SupplierApiError("rejected", {"code": 400})]):
            with self.assertRaises(SupplierApiError):
                client.buy_product("p", 2, "local-order")

    def test_status_without_secret_cannot_complete(self):
        client = self.client()
        with patch.object(client, "_request", return_value={"status": 1, "secret": ""}):
            self.assertEqual(client.query_order("ch5-" + "a" * 32 + "-1")["data"]["status"], "processing")

    def test_category_indices_and_stock(self):
        groups = bot.group_chuhai_products([{"categoryName": "A", "totalStock": 3}, {"categoryName": "B", "totalStock": 5}], "en")
        keyboard = bot.chuhai_keyboard(bot.build_rz_category_keyboard_localized(groups, 0, "en"), "en")
        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
        self.assertIn("cat5:0:0", callbacks)
        self.assertIn("cat5:1:0", callbacks)
        self.assertEqual(groups[1]["stock_total"], 5)

    def test_source_and_keyboard_isolation(self):
        self.assertEqual(bot.normalize_order_source("chuhai"), "chuhai")
        self.assertEqual(bot.account_list_number("chuhai", "en"), "5")
        self.assertEqual(bot.account_list_number("chuhai"), "\u2464")
        keyboard = bot.chuhai_keyboard(bot.build_rz_product_detail_keyboard_localized("abc", 0, 0, "zh"), "zh")
        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
        self.assertIn("qbuy5:abc:0:0", callbacks)
        self.assertFalse(any(callback.startswith(("prd3:", "qbuy3:", "cat3:")) for callback in callbacks))
        menu = bot.build_runtime_main_menu_inline_localized(SETTINGS, "zh", True)
        labels = {button.callback_data: button.text for row in menu.inline_keyboard for button in row}
        self.assertTrue(labels["nav:cats5"].endswith("".join(map(chr, [36134, 21495, 21015, 34920, 9316]))))


class SharedFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_quantity_generates_fifth_confirmation(self):
        context = make_context()
        context.application.bot_data["settings"] = replace(SETTINGS, chuhai_app_id="123", chuhai_app_key="key", chuhai_price_rate=1)
        client = ChuhaiSupplierClient(context.application.bot_data["settings"])
        context.application.bot_data["supplier_chuhai"] = client
        bot.set_pending_purchase(context, "abc", 0, 0, source="chuhai")
        update = make_update(text="2")
        update.message.reply_photo = AsyncMock()
        row = {"productId": "abc", "productName": "Account", "price": 1, "totalStock": 4, "source": "chuhai"}
        with patch.object(client, "get_product_detail", return_value={"data": row}):
            await bot.search_text_rich(update, context)
        self.assertEqual(context.user_data["chuhai_confirmation"], "cbuy5:abc:2:0:0")
        self.assertNotIn("tgstorex_confirmation", context.user_data)

    async def test_no_refund_on_query_error(self):
        context = make_context()
        client = ChuhaiSupplierClient(SETTINGS)
        context.application.bot_data["supplier_chuhai"] = client
        store = context.application.bot_data["store"]
        store.get_order.return_value = {"source": "chuhai", "created_at": "2020-01-01", "quantity": 2, "total_price": 2}
        with patch.object(client, "query_order", side_effect=SupplierApiError("unavailable")):
            result = await bot.finalize_remote_order(context, "ch5-" + "a" * 32 + "-2", False)
        self.assertEqual(result[0], "error")
        store.add_balance.assert_not_called()


if __name__ == "__main__":
    unittest.main()
