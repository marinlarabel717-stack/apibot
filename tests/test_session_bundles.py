import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import bot
from supplier_client import SupplierApiError
from test_account_lists import make_context, make_update, zip_bytes


def package(files, compression=zipfile.ZIP_STORED):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", compression=compression) as archive:
        for name, body in files:
            archive.writestr(name, body)
    return data.getvalue()


class SessionBundleTests(unittest.TestCase):
    def bundle_contents(self, packages):
        with tempfile.TemporaryDirectory() as directory, patch.object(bot, "DELIVERY_FILES_DIR", Path(directory)):
            entries = []
            for index, body in enumerate(packages):
                path = Path(directory) / f"input-{index}.zip"
                path.write_bytes(body)
                entries.append({"path": path})
            target = bot.build_tgstorex_delivery_bundle("order", entries)
            with zipfile.ZipFile(target) as archive:
                self.assertIsNone(archive.testzip())
                self.assertEqual(len(archive.namelist()), len(set(archive.namelist())))
                self.assertFalse(any(name.lower().endswith(".zip") for name in archive.namelist()))
                return {name: archive.read(name) for name in archive.namelist()}

    def test_nested_repeated_archives_and_repacked_duplicates_flatten(self):
        files = [("account/a.session", b"account-a"), ("account/a.json", b"metadata")]
        stored, compressed = package(files), package(files, zipfile.ZIP_DEFLATED)
        outer = package([("first.zip", stored), ("second.zip", stored), ("third.zip", compressed)])
        self.assertEqual(self.bundle_contents([outer, stored]), dict(files))

    def test_different_accounts_keep_tdata_together_on_name_collision(self):
        first = package([("tdata/key_data", b"shared"), ("tdata/account", b"first")])
        second = package([("tdata/key_data", b"shared"), ("tdata/account", b"second")])
        contents = self.bundle_contents([first, second])
        self.assertEqual(len(contents), 4)
        for name, body in contents.items():
            if name.endswith("/account"):
                self.assertIn(body, (b"first", b"second"))
                self.assertEqual(contents[name.rsplit("/", 1)[0] + "/key_data"], b"shared")

    def test_identical_metadata_in_different_account_directories_not_removed(self):
        files = [("account1/config.json", b"{}"), ("account2/config.json", b"{}")]
        self.assertEqual(self.bundle_contents([package(files)]), dict(files))

    def test_nested_archive_preserves_parent_folder(self):
        self.assertEqual(self.bundle_contents([package([("accounts/a.zip", zip_bytes("tdata/key_data"))])]),
                         {"accounts/tdata/key_data": b"session"})

    def test_unsafe_corrupt_or_excessive_nesting_is_rejected_without_partial_output(self):
        nested = zip_bytes()
        for _ in range(7):
            nested = package([("nested.zip", nested)])
        for body in (package([("../bad.session", b"bad")]), package([("bad.zip", b"not a zip")]), nested):
            with self.subTest(size=len(body)), tempfile.TemporaryDirectory() as directory, \
                 patch.object(bot, "DELIVERY_FILES_DIR", Path(directory)):
                source = Path(directory) / "input.zip"
                source.write_bytes(body)
                with self.assertRaises(SupplierApiError):
                    bot.build_tgstorex_delivery_bundle("order", [{"path": source}])
                self.assertFalse((Path(directory) / "order-bundle.zip").exists())
                self.assertFalse(list(Path(directory).glob("*.part")))

    def test_only_list_four_deduplicates_repeated_download_urls(self):
        context = make_context()
        payload = {"data": {"files": [
            {"downloadUrl": "https://tgstorex.com/file", "fileName": f"{index}.zip"} for index in range(3)
        ]}}
        row = {"raw_payload": json.dumps(payload)}
        entries = bot.build_delivery_entries(row, context.application.bot_data["supplier_tgstorex"])
        self.assertEqual(len(entries), 1)
        from supplier_client import DaxiSupplierClient
        self.assertEqual(len(bot.build_delivery_entries(row, DaxiSupplierClient(context.application.bot_data["settings"]))), 3)


class SessionBundleDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_order_redownload_removes_three_duplicate_zips_without_purchase(self):
        context = make_context()
        context.application.bot_data["store"].get_user_lang.return_value = "zh"
        order = {"task_id": "old-order", "user_id": 1, "source": "tgstorex", "file_url": "/file",
                 "quantity": 3, "delivery_sent_at": "2026-11-01"}
        sent = []
        async def capture(**kwargs):
            sent.append(kwargs["document"].read())
        context.bot.send_document.side_effect = capture
        with tempfile.TemporaryDirectory() as directory, patch.object(bot, "DELIVERY_FILES_DIR", Path(directory)):
            source = Path(directory) / "source.zip"
            source.write_bytes(package([("one.zip", zip_bytes()), ("two.zip", zip_bytes()), ("three.zip", zip_bytes())]))
            with patch.object(bot, "download_delivery_files_with_refresh", AsyncMock(return_value=([{"path": source}], order))):
                delivered = await bot.deliver_order_file(context, order, context.application.bot_data["supplier_tgstorex"],
                                                        include_ready_photo=False, force_resend=True)
        self.assertTrue(delivered)
        self.assertEqual(len(sent), 1)
        with zipfile.ZipFile(io.BytesIO(sent[0])) as archive:
            self.assertEqual(archive.namelist(), ["account/account.session"])
        context.application.bot_data["store"].debit_balance.assert_not_called()

    async def test_admin_download_uses_flattened_bundle_even_for_single_input(self):
        context, update = make_context(), make_update()
        order = {"task_id": "test", "source": "tgstorex", "file_url": "/file", "quantity": 3, "product_name": "test"}
        context.application.bot_data["store"].list_user_orders.return_value = [order]
        sent = []
        async def capture(**kwargs):
            sent.append(kwargs["document"].read())
        context.bot.send_document.side_effect = capture
        with tempfile.TemporaryDirectory() as directory, patch.object(bot, "DELIVERY_FILES_DIR", Path(directory)):
            source = Path(directory) / "source.zip"
            source.write_bytes(package([("inner.zip", zip_bytes())]))
            with patch.object(bot, "download_delivery_files_with_refresh", AsyncMock(return_value=([{"path": source}], order))):
                await bot.send_admin_cha_order_file(update, context, 1, 0)
        self.assertEqual(len(sent), 1)
        with zipfile.ZipFile(io.BytesIO(sent[0])) as archive:
            self.assertEqual(archive.namelist(), ["account/account.session"])
