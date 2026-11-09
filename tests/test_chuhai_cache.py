import asyncio
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
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


class AccountsOnlyTests(unittest.IsolatedAsyncioTestCase):
    setUp = CacheTests.setUp
    asyncTearDown = CacheTests.asyncTearDown
    def test_zero_stock_hidden_but_unknown_retained_and_callbacks_stable(self):
        rows = [dict(self.row, productId='fbzero', totalStock=0),
                dict(self.row, productId='igstock', categoryName='Instagram', totalStock=73),
                dict(self.row, productId='igzero', categoryName='Instagram', totalStock=0),
                dict(self.row, productId='igunknown', categoryName='Instagram', totalStock=0, stockUnknown=True)]
        groups = bot.group_chuhai_products(rows, 'zh')
        self.assertEqual([row['productId'] for row in groups[1]['rows']], ['igstock', 'igunknown'])
        self.assertEqual(groups[0]['stock_rows'][0]['productId'], 'fbzero')  # retained for restock checks
        keyboard = bot.chuhai_keyboard(bot.build_chuhai_category_keyboard_localized(groups, 0, 'zh'), 'zh')
        labels = {button.callback_data:button.text for line in keyboard.inline_keyboard for button in line}
        self.assertNotIn('cat5:0:0', labels)
        self.assertIn('cat5:1:0', labels)
        self.assertIn('至少 73', labels['cat5:1:0'])
        self.assertNotIn('更新中', labels['cat5:1:0'])
        keyboard = bot.build_chuhai_product_keyboard_localized(self.settings, rows, 0, 0, 'zh')
        callbacks = [b.callback_data for line in keyboard.inline_keyboard for b in line]
        self.assertFalse(any('fbzero' in c or 'igzero' in c for c in callbacks))
        self.assertTrue(any('igunknown' in c for c in callbacks))

    async def test_category_page_updates_partial_inventory_and_hides_empty_group(self):
        self.row.update(stockUnknown=True, totalStock=0)
        self.client._catalog['test'].update(stockUnknown=True, totalStock=0)
        message=SimpleNamespace(edit_reply_markup=AsyncMock())
        update=SimpleNamespace(callback_query=SimpleNamespace(message=message))
        tasks=[]
        self.context.application.create_task=lambda coroutine:tasks.append(asyncio.create_task(coroutine))
        async def tick(delay):
            self.client._catalog['test'].update(stockUnknown=False, totalStock=0)
        keyboard=bot.chuhai_keyboard(bot.build_chuhai_category_keyboard_localized(bot.group_chuhai_products([self.row],'zh'),0,'zh'),'zh')
        with patch.object(bot.asyncio,'sleep',side_effect=tick):
            bot.watch_chuhai_stock_page(update,self.context,self.settings,[self.row],[self.row],None,0,'zh',keyboard)
            await tasks[0]
        updated=message.edit_reply_markup.await_args.kwargs['reply_markup']
        self.assertFalse(any(b.callback_data == 'cat5:0:0' for line in updated.inline_keyboard for b in line))

    def test_original_lists_render_inventory_not_product_counts(self):
        group = {"index":0,"name":"Legacy category","stock_total":73,"product_count":999}
        for render in (bot.build_daxi_category_keyboard_localized, bot.build_rz_category_keyboard_localized):
            keyboard=render([group],0,'zh')
            text=keyboard.inline_keyboard[0][0].text
            self.assertIn('73',text)
            self.assertNotIn('999',text)
            self.assertNotIn('商品',text)
        keyboard=bot.build_chuhai_category_keyboard_localized([dict(group,stock_unknown=False)],0,'zh')
        text=keyboard.inline_keyboard[0][0].text
        self.assertIn('库存',text)
        self.assertIn('73',text)
        self.assertNotIn('999',text)

    def test_services_excluded_but_prepaid_and_follower_accounts_kept(self):
        allowed = [
            ("Facebook老号", "Facebook满五年老号"),
            ("TK 满月千粉未开橱窗", "TikTok 千粉账号 带作品"),
            ("TK满月白号", "德国满月白(outlook注册-带接码链接)"),
            ("Gmail邮箱", "Gmail老号带2FA"),
            ("OpenAI/ChatGPT", "GPT plus 成品号 账号已充值好一个月会员"),
        ]
        excluded = [
            ("Facebook 粉丝服务", "Facebook老号"),
            ("Instagram 流量", "IG account"),
            ("Facebook", "Facebook刷粉 1000粉丝"),
            ("Instagram", "Instagram点赞量"),
            ("TK", "TikTok 播放量服务"),
            ("OpenAI/ChatGPT", "Codex点数额度充值 Free账号勿下"),
            ("OpenAI/ChatGPT", "GPT续费月卡卡密"),
            ("Gmail", "美国长效API接码 绑定谷歌账户"),
            ("OpenAI/ChatGPT", "Github全新邮箱验证"),
        ]
        for category, name in allowed:
            self.assertTrue(self.client.account_product(category,name), (category,name))
        for category, name in excluded:
            self.assertFalse(self.client.account_product(category,name), (category,name))

    def test_old_snapshot_service_rows_are_removed(self):
        self.client._catalog['service'] = dict(self.row, productId='service', productName='Facebook刷粉服务')
        self.client._save_snapshot()
        restored=ChuhaiSupplierClient(self.settings)
        self.assertIsNone(restored.cached_product('service'))
        self.assertEqual(len(restored.cached_products()), 1)
        self.assertEqual(sum(len(group['rows']) for group in bot.group_chuhai_products(list(self.client._catalog.values()),'zh')), 1)

    def test_service_categories_do_not_request_product_endpoints(self):
        calls=[]
        def request(path,**params):
            calls.append((path,params))
            if path.endswith('/categories'):
                return [{'id':1,'name':'Facebook老号'},{'id':2,'name':'Facebook粉丝服务'},{'id':3,'name':'TikTok流量'}]
            self.assertEqual(params['category_id'],1)
            return {'items':[{'code':'good','name':'Facebook老号','factory_price':8,'delivery_way':0},
                             {'code':'bad','name':'FB代运营服务','factory_price':8,'delivery_way':0}]}
        with patch.object(self.client,'_request',side_effect=request):
            rows=self.client.get_products()['data']
        self.assertEqual([row['shared_code'] for row in rows],['good'])
        self.assertEqual(len(calls),2)

    async def test_current_page_updates_stock_without_another_click(self):
        self.row.update(stockUnknown=True,totalStock=0,source='chuhai')
        self.client._catalog['test'].update(stockUnknown=True,totalStock=0)
        message=SimpleNamespace(edit_reply_markup=AsyncMock())
        update=SimpleNamespace(callback_query=SimpleNamespace(message=message))
        tasks=[]
        self.context.application.create_task=lambda coroutine:tasks.append(asyncio.create_task(coroutine))
        async def tick(delay):
            self.client._catalog['test'].update(stockUnknown=False,totalStock=99)
        keyboard=bot.chuhai_keyboard(bot.build_chuhai_product_keyboard_localized(self.settings,[self.row],0,0,'zh'),'zh')
        with patch.object(bot.asyncio,'sleep',side_effect=tick):
            bot.watch_chuhai_stock_page(update,self.context,self.settings,[self.row],[self.row],0,0,'zh',keyboard)
            await tasks[0]
        message.edit_reply_markup.assert_awaited_once()
        self.assertIn('99',str(message.edit_reply_markup.await_args.kwargs['reply_markup'].to_dict()))

    async def test_navigating_away_stops_stock_page_edit(self):
        self.row.update(stockUnknown=True,totalStock=0,source='chuhai')
        message=SimpleNamespace(edit_reply_markup=AsyncMock())
        update=SimpleNamespace(callback_query=SimpleNamespace(message=message))
        tasks=[]
        self.context.application.create_task=lambda coroutine:tasks.append(asyncio.create_task(coroutine))
        keyboard=bot.chuhai_keyboard(bot.build_chuhai_product_keyboard_localized(self.settings,[self.row],0,0,'zh'),'zh')
        with patch.object(bot.asyncio,'sleep',AsyncMock()):
            bot.watch_chuhai_stock_page(update,self.context,self.settings,[self.row],[self.row],0,0,'zh',keyboard)
            self.context.user_data['chuhai_view_token']='navigated-away'
            await tasks[0]
        message.edit_reply_markup.assert_not_awaited()


if __name__ == '__main__': unittest.main()
