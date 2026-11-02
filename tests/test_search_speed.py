import asyncio
import time
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import bot
from supplier_client import SupplierApiError
from test_account_lists import make_context, make_update


class FastSearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for cache in (bot.CATALOG_SEARCH_CACHE, bot.DAXI_PRODUCTS_CACHE, bot.RZ_PRODUCTS_CACHE):
            cache.clear()
        bot.CATALOG_SEARCH_INCOMPLETE.clear()
        bot.CATEGORY_PRODUCTS_CACHE.clear()
        self.context = make_context()
        self.sources = (bot.LEGACY_SOURCE, bot.DAXI_SOURCE, bot.RZ_SOURCE, bot.TGSTOREX_SOURCE)

    async def asyncTearDown(self):
        tasks = list(self.context.application.bot_data.get("search_catalog_refresh_tasks", {}).values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def seed(self, source, rows, age=0):
        cache, key = bot.search_catalog_slot(self.context, source)
        cache[key] = (time.monotonic() - age, rows)

    def loaders(self, stack, loader):
        return [stack.enter_context(patch.object(bot, name, AsyncMock(side_effect=loader))) for name in (
            "build_catalog_search_rows", "fetch_daxi_products", "fetch_rz_products", "fetch_tgstorex_products",
        )]

    async def test_stale_snapshot_is_immediate_and_refresh_is_coalesced(self):
        old = [{"productId": "old"}]
        new = [{"productId": "new"}]
        release = asyncio.Event()
        async def slow_loader(*args, **kwargs):
            await release.wait()
            return new
        for source in self.sources:
            self.seed(source, old, age=120)
        with ExitStack() as stack:
            loaders = self.loaders(stack, slow_loader)
            started = time.perf_counter()
            catalogs, missing = await bot.get_fast_search_catalogs(self.context)
            self.assertLess(time.perf_counter() - started, 0.2)
            self.assertFalse(missing)
            self.assertTrue(all(rows is old for rows in catalogs.values()))
            await asyncio.sleep(0)
            tasks = list(self.context.application.bot_data["search_catalog_refresh_tasks"].values())
            for _ in range(20):
                again, _ = await bot.get_fast_search_catalogs(self.context)
                self.assertTrue(all(rows is old for rows in again.values()))
            self.assertTrue(all(loader.await_count == 1 for loader in loaders))
            release.set()
            await asyncio.gather(*tasks)
            catalogs, _ = await bot.get_fast_search_catalogs(self.context)
            self.assertTrue(all(rows is new for rows in catalogs.values()))

    async def test_failed_refresh_keeps_cache_and_backs_off(self):
        old = [{"productId": "old"}]
        for source in self.sources:
            self.seed(source, old, age=120)
        async def fail(*args, **kwargs):
            raise SupplierApiError("simulated outage")
        with ExitStack() as stack:
            loaders = self.loaders(stack, fail)
            with self.assertLogs("apibot", level="ERROR"):
                await bot.get_fast_search_catalogs(self.context)
                await asyncio.gather(*self.context.application.bot_data["search_catalog_refresh_tasks"].values())
            catalogs, missing = await bot.get_fast_search_catalogs(self.context)
            self.assertFalse(missing)
            self.assertTrue(all(rows is old for rows in catalogs.values()))
            self.assertTrue(all(loader.await_count == 1 for loader in loaders))

    async def test_cold_start_has_bounded_wait_and_keeps_refresh_alive(self):
        release = asyncio.Event()
        async def slow_loader(*args, **kwargs):
            await release.wait()
            return []
        with ExitStack() as stack:
            self.loaders(stack, slow_loader)
            stack.enter_context(patch.object(bot, "SEARCH_COLD_WAIT_SECONDS", 0.02))
            started = time.perf_counter()
            _, missing = await bot.get_fast_search_catalogs(self.context)
            self.assertLess(time.perf_counter() - started, 0.2)
            self.assertEqual(set(missing), set(self.sources))
            tasks = list(self.context.application.bot_data["search_catalog_refresh_tasks"].values())
            self.assertTrue(all(not task.done() for task in tasks))
            release.set()
            await asyncio.gather(*tasks)
            _, missing = await bot.get_fast_search_catalogs(self.context)
            self.assertFalse(missing)

    async def test_one_ready_supplier_does_not_wait_for_missing_suppliers(self):
        rows = [{"productId": "ready"}]
        self.seed(bot.TGSTOREX_SOURCE, rows)
        release = asyncio.Event()
        async def slow_loader(*args, **kwargs):
            await release.wait()
            return []
        with ExitStack() as stack:
            self.loaders(stack, slow_loader)
            started = time.perf_counter()
            catalogs, missing = await bot.get_fast_search_catalogs(self.context)
            self.assertLess(time.perf_counter() - started, 0.2)
            self.assertIs(catalogs[bot.TGSTOREX_SOURCE], rows)
            self.assertEqual(len(missing), 3)

    async def test_legacy_publishes_ready_categories_before_slow_category(self):
        supplier = self.context.application.bot_data["supplier"]
        release = asyncio.Event()
        ready = asyncio.Event()
        async def products(client, category_id, **kwargs):
            if category_id == 2:
                await release.wait()
            else:
                ready.set()
            return [{"productId": category_id, "productName": "Session"}]
        with patch.object(bot, "fetch_categories", AsyncMock(return_value=[
            {"categoryId": 1, "categoryName": "巴西", "prefix": "+55"},
            {"categoryId": 2, "categoryName": "日本"},
        ])), patch.object(bot, "fetch_category_products", AsyncMock(side_effect=products)):
            task = bot.ensure_search_catalog_refresh(self.context, bot.LEGACY_SOURCE)
            try:
                await ready.wait()
                rows = bot.CATALOG_SEARCH_CACHE[id(supplier)][1]
                self.assertEqual(rows[0]["categoryId"], 1)
                self.assertEqual(rows[0]["categoryName"], "巴西")
                self.assertTrue(bot.search_products_locally(rows, "巴西"))
                self.assertTrue(bot.search_products_locally(rows, "+55"))
                self.assertFalse(task.done())
            finally:
                release.set()
                await task
            self.assertNotIn(id(supplier), bot.CATALOG_SEARCH_INCOMPLETE)

    async def test_legacy_failed_category_keeps_previous_rows_and_new_successes(self):
        supplier = self.context.application.bot_data["supplier"]
        self.seed(bot.LEGACY_SOURCE, [
            {"productId": 1, "categoryId": 1, "productName": "old"},
            {"productId": 2, "categoryId": 2, "productName": "日本"},
        ], age=120)
        async def products(client, category_id, **kwargs):
            if category_id == 2:
                raise SupplierApiError("timeout")
            return [{"productId": 3, "productName": "巴西"}]
        for source in self.sources[1:]:
            self.seed(source, [])
        with patch.object(bot, "fetch_categories", AsyncMock(return_value=[
            {"categoryId": 1}, {"categoryId": 2},
        ])), patch.object(bot, "fetch_category_products", AsyncMock(side_effect=products)), self.assertLogs("apibot", level="ERROR"):
            await bot.ensure_search_catalog_refresh(self.context, bot.LEGACY_SOURCE)
        catalogs, missing = await bot.get_fast_search_catalogs(self.context)
        self.assertEqual({row["productId"] for row in catalogs[bot.LEGACY_SOURCE]}, {2, 3})
        self.assertIn(bot.LEGACY_SOURCE, missing)
        self.assertIn(bot.LEGACY_SOURCE, self.context.application.bot_data["search_catalog_failures"])
        self.assertIsNone(bot.ensure_search_catalog_refresh(self.context, bot.LEGACY_SOURCE))

    async def test_legacy_cold_failure_still_publishes_successful_categories(self):
        supplier = self.context.application.bot_data["supplier"]
        async def products(client, category_id, **kwargs):
            if category_id == 2:
                raise SupplierApiError("timeout")
            return [{"productId": 1, "country": "巴西", "productName": "Session"}]
        with patch.object(bot, "fetch_categories", AsyncMock(return_value=[
            {"categoryId": 1}, {"categoryId": 2},
        ])), patch.object(bot, "fetch_category_products", AsyncMock(side_effect=products)), self.assertLogs("apibot", level="ERROR"):
            with self.assertRaises(SupplierApiError):
                await bot.build_catalog_search_rows(supplier, refresh=True)
        rows = bot.CATALOG_SEARCH_CACHE[id(supplier)][1]
        self.assertEqual(len(bot.search_products_locally(rows, "巴西")), 1)
        self.assertIn(id(supplier), bot.CATALOG_SEARCH_INCOMPLETE)

    async def test_legacy_all_failed_does_not_publish_false_empty_snapshot(self):
        supplier = self.context.application.bot_data["supplier"]
        with patch.object(bot, "fetch_categories", AsyncMock(return_value=[{"categoryId": 1}])), patch.object(
            bot, "fetch_category_products", AsyncMock(side_effect=SupplierApiError("timeout"))
        ), self.assertLogs("apibot", level="ERROR"):
            with self.assertRaises(SupplierApiError):
                await bot.build_catalog_search_rows(supplier, refresh=True)
        self.assertNotIn(id(supplier), bot.CATALOG_SEARCH_CACHE)

    async def test_legacy_successful_empty_category_removes_previous_stock(self):
        supplier = self.context.application.bot_data["supplier"]
        self.seed(bot.LEGACY_SOURCE, [{"productId": 1, "categoryId": 1}])
        with patch.object(bot, "fetch_categories", AsyncMock(return_value=[{"categoryId": 1}])), patch.object(
            bot, "fetch_category_products", AsyncMock(return_value=[])
        ):
            rows = await bot.build_catalog_search_rows(supplier, refresh=True)
        self.assertEqual(rows, [])
        self.assertNotIn(id(supplier), bot.CATALOG_SEARCH_INCOMPLETE)

    def test_legacy_prefix_from_title_is_exact_and_country_fields_are_searchable(self):
        rows = [
            {"productId": 1, "productName": "3年 +55 巴西"},
            {"productId": 2, "productName": "3年 +550 Other"},
            {"productId": 3, "productName": "Session", "country_name": "巴西", "country_en": "Brazil"},
        ]
        self.assertEqual([row["productId"] for row in bot.search_products_locally(rows, "+55")], [1])
        self.assertEqual({row["productId"] for row in bot.search_products_locally(rows, "巴西")}, {1, 3})
        self.assertEqual([row["productId"] for row in bot.search_products_locally(rows, "Brazil")], [3])

    async def test_startup_schedules_all_catalogs_without_waiting_for_network(self):
        release = asyncio.Event()
        async def slow_loader(*args, **kwargs):
            await release.wait()
            return []
        with ExitStack() as stack:
            self.loaders(stack, slow_loader)
            stack.enter_context(patch.object(bot, "ensure_okpay_callback_server", AsyncMock()))
            started = time.perf_counter()
            await bot.on_application_post_init(self.context.application)
            self.assertLess(time.perf_counter() - started, 0.2)
            self.assertEqual(set(self.context.application.bot_data["search_catalog_refresh_tasks"]), set(self.sources))

    async def test_hot_search_handler_benchmark_and_no_keyword_network(self):
        count = 1000
        for source in self.sources:
            rows = [{"productId": index + 1, "productName": "巴西 Brazil", "country": "巴西", "prefix": "+55",
                     "totalStock": 50, "price": 2, "categoryId": 1, "categoryName": "Sessions",
                     "kind": "api_country", "source": source, "region": "americas"} for index in range(count)]
            if source == bot.TGSTOREX_SOURCE:
                rows = [dict(rows[0], productId="BR", country_en="Brazil")]
            self.seed(source, rows)
        update = make_update("巴西")
        with ExitStack() as stack:
            for name, value in (("ensure_user_with_lang", "zh"), ("handle_admin_text_input", False), ("should_ignore_for_closed_business", False)):
                stack.enter_context(patch.object(bot, name, AsyncMock(return_value=value)))
            loaders = self.loaders(stack, AssertionError("hot search must not query upstream"))
            timings = []
            for _ in range(5):
                started = time.perf_counter()
                await bot.search_text_rich(update, self.context)
                timings.append((time.perf_counter() - started) * 1000)
            self.assertTrue(all(loader.await_count == 0 for loader in loaders))
            self.context.application.bot_data["supplier"].search_products.assert_not_called()
            self.assertEqual(update.message.reply_text.await_count, 5)
            self.assertLess(max(timings), 500)
            print(f"BENCHMARK 3001 catalog rows: first={timings[0]:.1f}ms warm_min={min(timings[1:]):.1f}ms warm_max={max(timings[1:]):.1f}ms (mocked Telegram)", flush=True)

    def test_quantity_prompt_matches_requested_copy(self):
        expected = "✋请发送需要购买的数量：\n例如： 10"
        self.assertEqual(bot.ui_text("quick_buy_prompt", "zh"), expected)
        self.assertEqual(bot.ui_text("enter_quantity", "zh"), expected)


if __name__ == "__main__":
    unittest.main()
