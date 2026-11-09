import io
import itertools
import json
import os
import sys
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot
import config
from store import Store
from supplier_client import SupplierApiError
from tgstorex_client import COUNTRY_METADATA, TgstorexSupplierClient


with patch.dict(os.environ, {"BOT_TOKEN": "123456:test"}, clear=True), patch.object(config, "load_dotenv"):
    SETTINGS = replace(config.load_settings(), admin_user_ids={1}, tgstorex_api_key="test-token")

SOURCES = (bot.LEGACY_SOURCE, bot.DAXI_SOURCE, bot.RZ_SOURCE)


def make_context(store=None):
    return SimpleNamespace(
        application=SimpleNamespace(bot_data={
            "settings": SETTINGS, "store": store or MagicMock(), "supplier": MagicMock(),
            "supplier_daxi": MagicMock(), "supplier_rz": MagicMock(),
            "supplier_tgstorex": TgstorexSupplierClient(SETTINGS), "runtime_config": {},
        }), user_data={}, bot=SimpleNamespace(send_document=AsyncMock(), send_message=AsyncMock()),
    )


def make_update(text="德国", callback=None):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=1, username="tester", full_name="Tester"),
        message=SimpleNamespace(text=text, reply_text=AsyncMock()),
        callback_query=SimpleNamespace(data=callback, answer=AsyncMock()) if callback else None,
    )


def zip_bytes(name="account/account.session", content=b"session"):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(name, content)
    return stream.getvalue()


def response(body, content_type="application/zip", status=200):
    result = MagicMock(status_code=status, headers={"Content-Type": content_type})
    result.__enter__.return_value = result
    result.iter_content.return_value = [body]
    return result


class AccountListTests(unittest.IsolatedAsyncioTestCase):
    async def test_bottom_keyboard_restores_original_single_row(self):
        for lang, expected in (
            ("zh", [["主菜单", "联系客服", "充值余额"]]),
            ("en", [["Main Menu", "Support", "Balance"]]),
        ):
            keyboard = bot.build_menu_keyboard(lang)
            self.assertEqual([[button.text for button in row] for row in keyboard.keyboard], expected)
            self.assertTrue(keyboard.resize_keyboard)
            self.assertTrue(keyboard.is_persistent)
            self.assertTrue(bot.should_trigger_product_search("巴西"))

    async def test_broadcast_text_preserves_newlines_through_input_preview_and_delivery(self):
        text = "第一行\n\n第二行  保留空格\n第三行"
        context = make_context()
        update = make_update(text)
        await bot.prompt_admin_broadcast_content(update, context)
        with patch.object(bot, "handle_admin_business_toggle", AsyncMock(return_value=False)), \
             patch.object(bot, "handle_admin_legacy_supplier_toggle", AsyncMock(return_value=False)), \
             patch.object(bot, "should_ignore_for_closed_business", AsyncMock(return_value=False)), \
             patch.object(bot, "show_admin_broadcast_panel", AsyncMock()):
            await bot.search_text_rich(update, context)
        payload = bot.get_or_create_admin_broadcast_draft(context)["payload"]
        self.assertEqual(payload["text"], text)
        self.assertIn(text, bot.format_admin_broadcast_summary(payload))
        await bot.send_admin_preview(update, context, payload)
        self.assertEqual(update.message.reply_text.await_args.args[0], text)
        await bot.deliver_admin_payload(context.bot, 99, payload)
        self.assertEqual(context.bot.send_message.await_args.kwargs["text"], text)

    async def test_broadcast_photo_preserves_multiline_caption(self):
        text = "图片第一行\n\n图片第二行"
        context = make_context()
        update = make_update()
        update.message.photo = [SimpleNamespace(file_id="test-photo")]
        update.message.caption = text
        update.message.reply_photo = AsyncMock()
        context.bot.send_photo = AsyncMock()
        bot.set_pending_admin_action(context, {"kind": "broadcast_wait_content", "scope": bot.ADMIN_SEND_SCOPE_ALL})
        with patch.object(bot, "show_admin_broadcast_panel", AsyncMock()):
            await bot.handle_admin_photo_input(update, context)
        payload = bot.get_or_create_admin_broadcast_draft(context)["payload"]
        self.assertEqual(payload["text"], text)
        await bot.send_admin_preview(update, context, payload)
        self.assertEqual(update.message.reply_photo.await_args.kwargs["caption"], text)
        await bot.deliver_admin_payload(context.bot, 99, payload)
        self.assertEqual(context.bot.send_photo.await_args.kwargs["caption"], text)

    async def test_admin_purchase_notification_reports_exact_source(self):
        context = make_context()
        for source, number in zip((*SOURCES, bot.TGSTOREX_SOURCE), "①②③④"):
            context.bot.send_message.reset_mock()
            await bot.notify_admin_new_purchase(context, {1}, 7737512051, "buyer", "Buyer", "Brazil", 20, 30, 75.74, source=source)
            message = context.bot.send_message.await_args.kwargs
            self.assertIn(f"购买来源：账号列表{number}\n", message["text"])
            self.assertEqual(message["text"].count("购买来源"), 1)
            self.assertLess(message["text"].index("购买来源"), message["text"].index("购买商品"))
            entities = message["entities"]
            self.assertTrue(any(e.custom_emoji_id == "5879585266426973039" for e in entities))
            encoded = message["text"].encode("utf-16-le")
            self.assertTrue(any(e.type == "code" and encoded[e.offset * 2:(e.offset + e.length) * 2].decode("utf-16-le") == "7737512051" for e in entities))

    async def test_purchase_notification_matches_stored_source(self):
        context = make_context()
        store = context.application.bot_data["store"]
        store.debit_balance.return_value = (True, 98)
        row = {"productName": "test", "price": 2, "totalStock": 10}
        with patch.object(bot, "fetch_product_detail_cached", AsyncMock(return_value=row)), \
             patch.object(bot, "fetch_daxi_product_detail_cached", AsyncMock(return_value=row)), \
             patch.object(bot, "fetch_rz_product_detail_cached", AsyncMock(return_value=row)), \
             patch.object(bot, "notify_admin_new_purchase", AsyncMock()) as notify, \
             patch.object(bot, "schedule_fast_order_probe"):
            for source in (*SOURCES, bot.TGSTOREX_SOURCE):
                supplier = bot.get_supplier_for_source(context, source)
                supplier.get_product_detail = MagicMock(return_value={"data": row})
                supplier.buy_product = MagicMock(return_value={"data": {"orderId": "test-order", "taskId": "test-order"}})
                await bot.execute_purchase(context, 1, "buyer", "Buyer", "1", 1, source=source)
                if source == bot.DAXI_SOURCE:
                    notify.assert_not_awaited()
                else:
                    self.assertEqual(notify.await_args.kwargs["source"], source)
                notify.reset_mock()
                self.assertEqual(store.record_order.call_args.args[-1], source)
                self.assertEqual(store.record_order.call_args.kwargs["unit_cost"], row["price"])

    def test_all_menu_combinations(self):
        for enabled in itertools.product((False, True), repeat=5):
            expected = [cb for flag, cb in zip(enabled, ("nav:cats", "nav:cats2", "nav:cats3", "nav:cats4", "nav:cats5")) if flag]
            for lang in ("zh", "en"):
                menus = (
                    bot.build_runtime_main_menu_inline_localized(SETTINGS, lang, *enabled),
                    bot.build_runtime_profile_keyboard_localized(lang, *enabled),
                )
                for menu in menus:
                    rows = [[button.callback_data for button in row] for row in menu.inline_keyboard]
                    self.assertEqual([cb for row in rows for cb in row if cb.startswith("nav:cats")], expected)
                    self.assertTrue(all(len(row) <= 2 for row in rows))
                    if all(enabled):
                        self.assertEqual(rows[:2], [["nav:cats", "nav:cats2"], ["nav:cats3", "nav:cats4"]])

    async def test_admin_switches_persist_independently(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            store = Store(Path(directory) / "test.db")
            context = make_context(store)
            update = make_update()
            with patch.object(bot, "ensure_user_with_lang", AsyncMock(return_value="zh")), patch.object(bot, "send_menu_message", AsyncMock()):
                all_sources = (*SOURCES, bot.TGSTOREX_SOURCE)
                for index, source in enumerate(all_sources, 1):
                    self.assertTrue(await bot.handle_admin_legacy_supplier_toggle(update, context, f"关闭账号列表{index}"))
                    self.assertFalse(bot.effective_account_list_enabled(context, source))
                self.assertTrue(await bot.handle_admin_legacy_supplier_toggle(update, context, "开启 账号列表②"))
                persisted = store.get_runtime_settings()
                self.assertEqual([persisted[bot.account_list_runtime_key(s)] for s in all_sources], ["0", "1", "0", "0"])
                context.application.bot_data["runtime_config"] = persisted
                self.assertEqual([bot.effective_account_list_enabled(context, s) for s in all_sources], [False, True, False, False])
                self.assertTrue(await bot.handle_admin_legacy_supplier_toggle(update, context, "开启 账号列表④"))
                self.assertTrue(bot.effective_account_list_enabled(context, bot.TGSTOREX_SOURCE))
                persisted = store.get_runtime_settings()
                update.effective_user.id = 99
                self.assertFalse(await bot.handle_admin_legacy_supplier_toggle(update, context, "关闭账号列表4"))
                self.assertEqual(store.get_runtime_settings(), persisted)

    async def test_disabled_callbacks_quantity_and_purchase_are_blocked(self):
        context = make_context()
        for source, suffix in zip((*SOURCES, bot.TGSTOREX_SOURCE), ("", "2", "3", "4")):
            context.application.bot_data["runtime_config"][bot.account_list_runtime_key(source)] = "0"
            for action in (f"nav:cats{suffix}", f"prd{suffix}:1:0:0", f"qbuy{suffix}:1:0:0", f"cbuy{suffix}:1:2:0:0"):
                with patch.object(bot, "should_ignore_for_closed_business", AsyncMock(return_value=False)), patch.object(bot, "reply_account_list_disabled", AsyncMock()) as disabled:
                    await bot.on_callback(make_update(callback=action), context)
                    disabled.assert_awaited_once()
            result = await bot.execute_purchase(context, 1, "tester", "Tester", "1", 1, source=source)
            self.assertIn("已关闭", result[0])
            context.application.bot_data["store"].debit_balance.assert_not_called()
            bot.set_pending_purchase(context, "1", 0, 0, source)
            with patch.object(bot, "ensure_user_with_lang", AsyncMock(return_value="zh")), patch.object(bot, "reply_account_list_disabled", AsyncMock()) as disabled:
                await bot.search_text_rich(make_update("2"), context)
                disabled.assert_awaited_once()
                self.assertIsNone(bot.get_pending_purchase(context))

    async def test_closed_lists_are_not_searched_even_when_no_matches(self):
        context = make_context()
        for source in SOURCES:
            context.application.bot_data["runtime_config"][bot.account_list_runtime_key(source)] = "0"
        with patch.object(bot, "ensure_user_with_lang", AsyncMock(return_value="zh")), \
             patch.object(bot, "should_ignore_for_closed_business", AsyncMock(return_value=False)), \
             patch.object(bot, "handle_admin_text_input", AsyncMock(return_value=False)), \
             patch.object(bot, "fetch_daxi_products", AsyncMock()) as daxi, \
             patch.object(bot, "fetch_rz_products", AsyncMock()) as rz, \
             patch.object(bot, "search_products_cached", AsyncMock()) as legacy, \
             patch.object(bot, "fetch_tgstorex_products", AsyncMock(return_value=[])) as fourth:
            await bot.search_text_rich(make_update(), context)
            for fetch in (daxi, rz, legacy):
                fetch.assert_not_awaited()
            fourth.assert_awaited_once()

    async def test_existing_delivery_still_works_when_lists_closed(self):
        context = make_context()
        context.application.bot_data["store"].get_user_lang.return_value = "zh"
        for source in (*SOURCES, bot.TGSTOREX_SOURCE):
            context.application.bot_data["runtime_config"][bot.account_list_runtime_key(source)] = "0"
        order = {"task_id": "test-order", "file_url": "https://tgstorex.com/file", "user_id": 1, "quantity": 1}
        with tempfile.TemporaryDirectory() as directory, patch.object(bot, "DELIVERY_FILES_DIR", Path(directory)):
            path = Path(directory) / "original.zip"
            original = zip_bytes()
            path.write_bytes(original)
            sent = []
            async def capture(**kwargs):
                sent.append(kwargs["document"].read())
            context.bot.send_document.side_effect = capture
            supplier = context.application.bot_data["supplier_tgstorex"]
            entries = [{"path": path, "file_name": "original.zip", "file_url": order["file_url"]}]
            with patch.object(bot, "download_delivery_files_with_refresh", AsyncMock(return_value=(entries, order))):
                self.assertTrue(await bot.deliver_order_file(context, order, supplier, include_ready_photo=False))
                order["delivery_sent_at"] = "2026-11-01"
                self.assertTrue(await bot.deliver_order_file(context, order, supplier, include_ready_photo=False))
                self.assertEqual(len(sent), 1)
                self.assertTrue(await bot.deliver_order_file(context, order, supplier, include_ready_photo=False, force_resend=True))
            self.assertEqual(len(sent), 2)
            for body in sent:
                with zipfile.ZipFile(io.BytesIO(body)) as archive:
                    self.assertEqual(archive.namelist(), ["account/account.session"])
                    self.assertEqual(archive.read("account/account.session"), b"session")

    async def test_redownload_command_checks_ownership_and_never_purchases(self):
        context = make_context()
        context.application.bot_data["runtime_config"][bot.account_list_runtime_key(bot.TGSTOREX_SOURCE)] = "0"
        context.args = ["existing-order", "download"]
        local_order = {"task_id": "existing-order", "user_id": 2, "source": "tgstorex", "file_url": "/file", "delivery_sent_at": "2026-11-01"}
        store = context.application.bot_data["store"]
        store.get_order.return_value = local_order
        update = make_update()
        with patch.object(bot, "ensure_user_with_lang", AsyncMock(return_value="zh")), \
             patch.object(bot, "should_ignore_for_closed_business", AsyncMock(return_value=False)), \
             patch.object(bot, "finalize_remote_order", AsyncMock(return_value=("completed", "done"))) as finalize, \
             patch.object(bot, "deliver_order_file", AsyncMock(return_value=True)) as deliver:
            update.effective_user.id = 99
            await bot.order(update, context)
            finalize.assert_not_awaited()
            deliver.assert_not_awaited()
            update.effective_user.id = 2
            await bot.order(update, context)
            self.assertTrue(deliver.await_args.kwargs["force_resend"])
            store.debit_balance.assert_not_called()

    async def test_list_four_closed_catalog_and_direct_callbacks_never_fetch(self):
        context = make_context()
        context.application.bot_data["runtime_config"][bot.account_list_runtime_key(bot.TGSTOREX_SOURCE)] = "0"
        with patch.object(bot, "ensure_user_with_lang", AsyncMock(return_value="zh")), \
             patch.object(bot, "reply_account_list_disabled", AsyncMock()) as disabled, \
             patch.object(bot, "fetch_tgstorex_products", AsyncMock()) as fetch:
            await bot.route_menu_text(make_update("账号列表④"), context)
            await bot.on_tgstorex_callback(make_update(callback="qbuy4:BR:0:0"), context, ["qbuy4", "BR", "0", "0"])
        self.assertEqual(disabled.await_count, 2)
        fetch.assert_not_awaited()

    async def test_all_four_closed_do_not_search_or_start_refresh(self):
        context = make_context()
        for source in (*SOURCES, bot.TGSTOREX_SOURCE):
            context.application.bot_data["runtime_config"][bot.account_list_runtime_key(source)] = "0"
        with patch.object(bot, "ensure_search_catalog_refresh") as refresh:
            catalogs, missing = await bot.get_fast_search_catalogs(context)
        self.assertEqual(catalogs, {})
        self.assertEqual(missing, [])
        refresh.assert_not_called()


class SessionFileTests(unittest.TestCase):
    def setUp(self):
        self.client = TgstorexSupplierClient(SETTINGS)

    def test_iso_country_mapping_without_names_or_prefixes(self):
        samples = {"AE": ("asia", "+971"), "AF": ("asia", "+93"), "AG": ("americas", "+1268"), "DE": ("europe", "+49"), "NG": ("africa", "+234"), "NZ": ("oceania", "+64")}
        rows = []
        for code, (region, prefix) in samples.items():
            row = self.client._normalize_product_row({"code": code, "type": "number", "price": 1, "quantity": 10}, shared_catalog=True)
            self.assertEqual(row["region"], region)
            self.assertEqual(row["prefix"], prefix)
            self.assertNotEqual(row["country"], code)
            rows.append(row)
        self.assertEqual({g["key"] for g in bot.group_tgstorex_products(rows, "zh")}, {"region_" + r for r, _ in samples.values()})
        self.assertGreater(len(COUNTRY_METADATA), 240)

    def test_valid_zip_preserved_and_bad_cache_refetched(self):
        original = zip_bytes()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "cached.zip"
            target.write_bytes(b"<html>login</html>")
            self.client.session.get = MagicMock(return_value=response(original))
            self.client.download_session_file("/orders/test/file", target)
            self.assertEqual(target.read_bytes(), original)
            self.client.download_session_file("/orders/test/file", target)
            self.client.session.get.assert_called_once()
            self.assertFalse(self.client.session.get.call_args.kwargs["allow_redirects"])

    def test_html_json_redirect_truncated_and_crc_errors_rejected(self):
        corrupt_crc = zip_bytes(content=b"test-session").replace(b"test-session", b"bad!-session")
        for body, content_type, status in (
            (b"<html>login</html>", "text/html", 200),
            (b'{"error":"unauthorized"}', "application/json", 200),
            (b"login", "application/octet-stream", 302),
            (zip_bytes()[:40], "application/zip", 200),
            (corrupt_crc, "application/zip", 200),
        ):
            with self.subTest(content_type=content_type, status=status), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "bad.zip"
                self.client.session.get = MagicMock(return_value=response(body, content_type, status))
                with self.assertRaises(SupplierApiError):
                    self.client.download_session_file("/orders/test/file", target)
                self.assertFalse(target.exists())
                self.assertFalse(target.with_suffix(".zip.part").exists())

    def test_download_follows_ready_descriptor_to_original_zip(self):
        original = zip_bytes()
        descriptor = {"data": {"order_id": "test", "ready": True, "status": "done", "message": "ready",
                                "file_url": "?download=1", "file_path": "downloads/test.zip"}}
        self.client.session.get = MagicMock(side_effect=[
            response(json.dumps(descriptor).encode(), "application/json"), response(original),
        ])
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "result.zip"
            self.client.download_session_file("/api/v1/orders/test/file", target)
            self.assertEqual(target.read_bytes(), original)
        calls = self.client.session.get.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].args[0], "https://tgstorex.com/api/v1/orders/test/file?download=1")
        self.assertTrue(all(c.kwargs["headers"]["Authorization"] == "Bearer test-token" for c in calls))

    def test_descriptor_does_not_leak_credentials_or_loop(self):
        for data in (
            {"ready": True, "file_url": "https://evil.example/stolen"},
            {"ready": True, "file_url": "http://127.0.0.1/private"},
            {"ready": False, "file_url": "?download=1"},
            {"ready": True, "order_id": "other-order", "file_url": "?download=1"},
        ):
            with self.subTest(data=data), tempfile.TemporaryDirectory() as directory:
                self.client.session.get = MagicMock(return_value=response(json.dumps({"data": data}).encode(), "application/json"))
                with self.assertRaises(SupplierApiError):
                    self.client.download_session_file("/api/v1/orders/test/file", Path(directory) / "result.zip")
                self.client.session.get.assert_called_once()

    def test_self_descriptor_tries_download_mode_once(self):
        descriptor = {"data": {"order_id": "test", "ready": True,
                                "file_url": "/api/v1/orders/test/file"}}
        original = zip_bytes()
        self.client.session.get = MagicMock(side_effect=[
            response(json.dumps(descriptor).encode(), "application/json"), response(original),
        ])
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "result.zip"
            self.client.download_session_file(descriptor["data"]["file_url"], target)
            self.assertEqual(target.read_bytes(), original)
        self.assertEqual(self.client.session.get.call_count, 2)
        self.assertEqual(self.client.session.get.call_args.args[0],
                         "https://tgstorex.com/api/v1/orders/test/file?download=1")

    def test_download_mode_cannot_loop_or_modify_other_queries(self):
        for path, expected_calls in (
            ("/api/v1/orders/test/file", 2),
            ("/api/v1/orders/test/file?download=1", 1),
            ("/api/v1/orders/test/file?signature=preserve", 1),
            ("/storage/test.zip", 1),
        ):
            with self.subTest(path=path), tempfile.TemporaryDirectory() as directory:
                body = json.dumps({"data": {"ready": True, "file_url": path}}).encode()
                self.client.session.get = MagicMock(return_value=response(body, "application/json"))
                target = Path(directory) / "result.zip"
                with self.assertRaises(SupplierApiError):
                    self.client.download_session_file(path, target)
                self.assertEqual(self.client.session.get.call_count, expected_calls)
                self.assertFalse(target.exists())

    def test_duplicate_packages_are_merged_once_without_nested_zip(self):
        original = zip_bytes("tdata/key_data")
        with tempfile.TemporaryDirectory() as directory, patch.object(bot, "DELIVERY_FILES_DIR", Path(directory)):
            entries = []
            for index in (1, 2, 3):
                path = Path(directory) / f"source{index}.zip"
                path.write_bytes(original)
                entries.append({"path": path, "file_name": path.name})
            bundle = bot.build_tgstorex_delivery_bundle("order", entries)
            with zipfile.ZipFile(bundle) as archive:
                self.assertEqual(archive.namelist(), ["tdata/key_data"])
                self.assertEqual(archive.read("tdata/key_data"), b"session")


if __name__ == "__main__":
    unittest.main()
