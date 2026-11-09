import asyncio
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import bot
from chuhai_client import ChuhaiSupplierClient
from test_account_lists import SETTINGS, make_context


class CacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.settings = replace(SETTINGS, chuhai_app_id='cache-user', chuhai_app_key='cache-key', database_path=Path(temporary.name)/'bot.sqlite')
        self.client = ChuhaiSupplierClient(self.settings)
        self.context = make_context()
        self.context.application.bot_data.update(settings=self.settings, supplier_chuhai=self.client)
        self.row = dict(productId='test', shared_code='upstream', race='', categoryName='Facebook', productName='Account', price=2, totalStock=42, stockUnknown=False)
        self.client._catalog = {'test': dict(self.row)}
        self.slot = (id(self.client), '')
        self.addCleanup(lambda: bot.RZ_PRODUCTS_CACHE.pop(self.slot, None))

    async def asyncTearDown(self):
        state = self.context.application.bot_data
        tasks = list(state.get("search_catalog_refresh_tasks", {}).values())
        if state.get("chuhai_stock_task"):
            tasks.append(state["chuhai_stock_task"])
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_stale_catalog_is_immediate_and_refresh_is_shared(self):
        rows = [dict(self.row)]
        bot.RZ_PRODUCTS_CACHE[self.slot] = (time.monotonic()-600, rows)
        gate = asyncio.Event()
        started = asyncio.Event()
        async def slow(*args):
            started.set()
            await gate.wait()
            return {'data': [dict(self.row, price=3)]}
        with patch.object(bot, 'call_blocking', side_effect=slow) as upstream:
            for _ in range(10):
                result = await asyncio.wait_for(bot.fetch_chuhai_products(self.context), .2)
                self.assertIs(result, rows)
            task = self.context.application.bot_data['search_catalog_refresh_tasks']['chuhai']
            await asyncio.wait_for(started.wait(), .2)
            self.assertEqual(upstream.call_count, 1)
            gate.set()
            await task
        self.assertEqual(bot.RZ_PRODUCTS_CACHE[self.slot][1][0]['price'], 3)

    async def test_stock_loading_never_waits_for_api_and_deduplicates(self):
        self.row['stockUnknown'] = True
        self.client._catalog['test']['stockUnknown'] = True
        gate = asyncio.Event()
        started = asyncio.Event()
        async def slow(*args):
            started.set()
            await gate.wait()
            return [dict(self.row, stockUnknown=False, totalStock=99)]
        with patch.object(bot, 'call_blocking', side_effect=slow) as upstream:
            for _ in range(10):
                await asyncio.wait_for(bot.refresh_chuhai_visible_inventory(self.context, [self.row]), .2)
            await asyncio.wait_for(started.wait(), .2)
            self.assertEqual(upstream.call_count, 1)
            task = self.context.application.bot_data['chuhai_stock_task']
            gate.set()
            await task
            self.assertEqual(self.row['totalStock'], 99)
            await bot.refresh_chuhai_visible_inventory(self.context, [self.row])
            self.assertEqual(upstream.call_count, 1)

    async def test_refresh_failure_keeps_previous_snapshot(self):
        rows = [dict(self.row)]
        bot.RZ_PRODUCTS_CACHE[self.slot] = (0, rows)
        with patch.object(bot, 'call_blocking', side_effect=RuntimeError('offline')):
            self.assertIs(await bot.fetch_chuhai_products(self.context), rows)
            task = self.context.application.bot_data['search_catalog_refresh_tasks']['chuhai']
            await task
        self.assertIs(bot.RZ_PRODUCTS_CACHE[self.slot][1], rows)

    def test_disk_snapshot_survives_restart_and_is_account_and_rate_scoped(self):
        self.client._save_snapshot()
        restored = ChuhaiSupplierClient(self.settings)
        self.assertEqual(restored.cached_products()[0]['totalStock'], 42)
        with patch.object(restored, '_request', side_effect=AssertionError('Browsing must be offline')):
            self.assertEqual(restored.cached_product('test')['price'], 2)
        for settings in [replace(self.settings, chuhai_app_id='other'), replace(self.settings, chuhai_cny_per_usdt=9)]:
            self.assertEqual(ChuhaiSupplierClient(settings).cached_products(), [])

    def test_corrupt_disk_snapshot_is_ignored(self):
        self.client._snapshot_path().write_text('{"rows":[123]}', encoding='utf-8')
        restored = ChuhaiSupplierClient(self.settings)
        self.assertEqual(restored.cached_products(), [])

    def test_only_allowed_category_products_are_requested_and_sorted(self):
        names = ['TikTok', 'OpenAI/ChatGPT', 'Gmail', 'Instagram-IG-ins', 'Facebook', 'Twitter', 'Threads-IG-ins', 'Telegram']
        calls=[]
        def request(path, **params):
            calls.append((path, params))
            if path.endswith('/categories'):
                return [{'id':index,'name':name} for index,name in enumerate(names)]
            self.assertTrue(path.endswith('/categoryItems'))
            self.assertLess(params['category_id'], 5)
            return {'items':[dict(code=str(params['category_id']),name='account',factory_price=8,delivery_way=0)]}
        with patch.object(self.client, '_request', side_effect=request):
            rows = self.client.get_products()['data']
        self.assertEqual(len(rows), 5)
        self.assertEqual(len(calls), 6)
        self.assertFalse(any(path.endswith('/items') for path,_ in calls))
        self.assertEqual([group['key'] for group in bot.group_chuhai_products(rows, 'zh')], ['fb','ig','gmail','gpt','tk'])


if __name__ == '__main__': unittest.main()
