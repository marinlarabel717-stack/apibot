from __future__ import annotations

import hashlib
import configparser
import json
import math
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode, unquote_plus

import requests
from supplier_client import SupplierApiError, SupplierClient


class ChuhaiSupplierClient(SupplierClient):
    """Shared API adapter. Local order IDs retain request_no + quantity for recovery."""

    PLATFORM_NAMES = {"fb": "FB · Facebook", "ig": "IG · Instagram", "gmail": "Gmail", "gpt": "GPT · ChatGPT", "tk": "TK · TikTok"}

    @classmethod
    def platform(cls, name):
        name = str(name or "").casefold()
        if "threads" in name:
            return None
        if "facebook" in name or "脸书" in name or re.search(r"\bfb\b", name):
            return "fb"
        if "instagram" in name or re.search(r"\b(ig|ins)\b", name):
            return "ig"
        if "gmail" in name:
            return "gmail"
        if "chatgpt" in name or "openai" in name or re.search(r"\bgpt\b", name):
            return "gpt"
        if "tiktok" in name or re.match(r"^tk(?:\b|[^a-z])", name):
            return "tk"
        return None

    @classmethod
    def account_category(cls, name):
        text = str(name or "").casefold()
        return bool(cls.platform(text)) and not re.search(
            r"服务|刷粉|涨粉|刷量|流量|推广|引流|followers|traffic|views|likes|boost|marketing|services?|smm|engagement", text)

    @classmethod
    def account_product(cls, category, name):
        if not cls.account_category(category):
            return False
        text = str(name or "").casefold()
        # Prepaid account sales are accounts, whereas charging an existing account is a service.
        text = re.sub(r"已充值(?:好)?|已开通|已订阅|非充值|带接码链接|接码链接|支持接码|可接码|自然流量|带流量|流量号|售后服务", "", text)
        excluded = (r"刷粉|涨粉|买粉|刷量|粉丝服务|流量|播放量|浏览量|点赞量|转发量|评论量|"
                    r"充值|代充|直充|续费|代开|代注册|接码|验证码|卡密|月卡|年卡|礼品卡|cdk|"
                    r"中转|额度|服务|代运营|代养|软件|工具|教程|课程|解封|申诉|广告投放|引流|"
                    r"公共主页|粉丝页|followers|traffic|views|likes|boost|top.?up|recharge|"
                    r"api[ _-]?key|access[ _-]?token|activation[ _-]?code|gift[ _-]?card|services?|renewal|sms|credits")
        if re.search(excluded, text):
            return False
        if re.search(r"github|suno|claude", text) and not cls.platform(name):
            return False
        if cls.platform(category) == "gmail" and re.search(r"教育|\bedu\b|\beud\b", text) and "gmail" not in text:
            return False
        return True

    CATALOG_TTL = 300
    INVENTORY_TTL = 120

    def __post_init__(self):
        super().__post_init__()
        self._snapshot_lock = threading.RLock()
        self._inventory_cache = {}
        self._load_snapshot()

    def _snapshot_path(self):
        identity = f"platform-cache-v2|{self.settings.chuhai_api_base_url}|{self.settings.chuhai_app_id}|{self.settings.chuhai_app_key}|{getattr(self.settings, 'chuhai_cny_per_usdt', 0)}|{self.settings.chuhai_price_rate}"
        key = hashlib.sha256(identity.encode()).hexdigest()[:20]
        return self.settings.database_path.parent / ("chuhai-cache-" + key + ".json")

    def _load_snapshot(self):
        try:
            data = json.loads(self._snapshot_path().read_text(encoding="utf-8"))
            rows = data["rows"]
            if not isinstance(rows, list) or any(not isinstance(row, dict) or not row.get("productId") for row in rows):
                return
            rows = [row for row in rows if self.account_product(row.get("categoryName"), row.get("productName"))]
            self._catalog = {row["productId"]: row for row in rows}
        except (OSError, ValueError, TypeError, KeyError):
            pass

    def _save_snapshot(self):
        try:
            with self._snapshot_lock:
                target = self._snapshot_path()
                target.parent.mkdir(parents=True, exist_ok=True)
                temp = target.with_suffix(".tmp")
                temp.write_text(json.dumps({"rows": list(self._catalog.values())}, ensure_ascii=False), encoding="utf-8")
                temp.replace(target)
        except OSError:
            pass  # A read-only disk must not prevent browsing or buying.

    def cached_products(self):
        return [row for row in getattr(self, "_catalog", {}).values() if self.account_product(row.get("categoryName"), row.get("productName"))]

    def cached_product(self, product_id):
        row = getattr(self, "_catalog", {}).get(str(product_id))
        return dict(row) if row and self.account_product(row.get("categoryName"), row.get("productName")) else None

    @property
    def configured(self):
        return bool(self.settings.chuhai_app_id and self.settings.chuhai_app_key)

    def _request(self, path, **params):
        if not self.configured:
            raise SupplierApiError("Account List 5: CHUHAI_APP_ID / CHUHAI_APP_KEY not configured")
        request_timeout = params.pop("_request_timeout", self.settings.chuhai_api_timeout_seconds)
        data = {"app_id": self.settings.chuhai_app_id, **params}
        data = {key: value for key, value in sorted(data.items()) if value != ""}
        # Match PHP md5(urldecode(http_build_query(params) . '&key=' . key)).
        signing = unquote_plus(urlencode(data) + "&key=" + self.settings.chuhai_app_key)
        data["sign"] = hashlib.md5(signing.encode("utf-8")).hexdigest()
        try:
            response = self.session.post(
                self.settings.chuhai_api_base_url + path, data=data,
                timeout=request_timeout, allow_redirects=False,
            )
            response.raise_for_status()
            result = response.json()
        except (requests.RequestException, ValueError):
            raise SupplierApiError("Shared API response unavailable") from None
        if not isinstance(result, dict) or str(result.get("code")) != "200":
            raise SupplierApiError("Shared API request rejected", result)
        return result.get("data")

    def query_balance(self):
        data = self._request("/shared/authentication/connect")
        return {"success": True, "data": {"accountBalance": float(data["balance"]) * self._rate(), "currency": "USDT", "sourceBalance": data["balance"], "sourceCurrency": "CNY", "userName": data.get("shopName", "")}}

    def _rate(self):
        cny_per_usdt = getattr(self.settings, "chuhai_cny_per_usdt", 0)
        value = 1.0 / cny_per_usdt if cny_per_usdt > 0 else self.settings.chuhai_price_rate
        if not math.isfinite(value) or value <= 0:
            raise SupplierApiError("Account List 5: configure CHUHAI_PRICE_RATE (upstream currency to local currency)")
        return value

    def get_products(self, query=""):
        try:
            return self._get_products(query)
        except (TypeError, ValueError, KeyError, AttributeError, OverflowError, configparser.Error):
            raise SupplierApiError("Invalid Shared API product data") from None

    @staticmethod
    def parse_config(value):
        if not value:
            return {}
        if isinstance(value, dict):
            return value
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError):
            pass
        parser = configparser.RawConfigParser(strict=False, delimiters=("=",))
        parser.optionxform = str
        parser.read_string(str(value))
        return {section: dict(parser.items(section)) for section in parser.sections()}

    def _get_products(self, query=""):
        rate = self._rate()
        categories = self._request("/shared/commodity/categories")
        if not isinstance(categories, list):
            raise SupplierApiError("Invalid Shared API category array")
        selected = [category for category in categories if self.account_category(category.get("name"))]
        def fetch_category(category):
            payload = self._request("/shared/commodity/categoryItems", category_id=category["id"])
            if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                raise SupplierApiError("Invalid Shared API category products")
            return {**category, "children": payload["items"]}
        with ThreadPoolExecutor(max_workers=3) as executor:
            data = list(executor.map(fetch_category, selected))
        rows = []
        for category in data:
            for item in category.get("children") or []:
                if not self.account_product(category.get("name"), item.get("name")):
                    continue
                code = str(item.get("code") or item.get("shared_code") or "")
                reason = ""
                try:
                    config = self.parse_config(item.get("config"))
                except (ValueError, configparser.Error):
                    config, reason = {}, "商品配置暂无法解析"
                widget = item.get("widget") or []
                if isinstance(widget, str):
                    try:
                        widget = json.loads(widget)
                    except ValueError:
                        pass
                if widget or config.get("widget"):
                    reason = "该商品需要额外填写资料，暂不支持自动下单"
                if item.get("draft_status") in (1, "1"):
                    reason = "该商品需要预选卡密，暂不支持自动下单"
                if str(item.get("delivery_way", "")) != "0":
                    reason = "该商品为人工或插件发货，暂不支持自动下单"
                if not code:
                    reason = "该商品未提供API商品代码"
                specs = config.get("category_factory") or {}
                if not isinstance(specs, dict):
                    specs, reason = {}, "该商品规格配置暂无法解析"
                prices = specs.items() if specs else [("", item.get("factory_price"))]
                for race, price in prices:
                    blocked = reason
                    try:
                        cost = float(price)
                    except (ValueError, TypeError):
                        cost = 0
                    if not math.isfinite(cost) or cost <= 0:
                        cost, blocked = 0, "该商品成本价待确认，暂不可购买"
                    identity = code or "item-" + str(item.get("id"))
                    product_id = hashlib.sha256((identity + "\0" + str(race)).encode()).hexdigest()[:20]
                    rows.append({"productId": product_id, "productName": str(item.get("name") or identity) + (" / " + str(race) if race else ""),
                                 "categoryName": category.get("name", ""), "section": category.get("name", ""), "platform": self.platform(category.get("name")),
                                 "kind": "other", "price": cost * rate, "totalStock": 0,
                                 "source": "chuhai", "stockUnknown": True, "purchaseBlocked": blocked,
                                 "priceUnknown": not bool(cost), "minimum": item.get("minimum"), "maximum": item.get("maximum"),
                                 "shared_code": code, "race": str(race)})
        with self._snapshot_lock:
            previous = getattr(self, "_catalog", {})
            for row in rows:
                old = previous.get(row["productId"], {})
                if not old.get("stockUnknown", True):
                    row["totalStock"] = old["totalStock"]
                    row["stockUnknown"] = False
            self._catalog = {row["productId"]: row for row in rows}
            self._save_snapshot()
        return {"success": True, "data": rows}

    def refresh_inventory(self, product_ids):
        """Bounded on-demand inventory for one visible page, never the full catalog."""
        cache = getattr(self, "_inventory_cache", None)
        if cache is None:
            self._inventory_cache = cache = {}
        def fetch(product_id):
            row = dict(self._catalog[product_id])
            cached = cache.get(product_id)
            if cached and time.monotonic() - cached[0] < self.INVENTORY_TTL:
                return cached[1]
            try:
                data = self._request("/shared/commodity/inventory", sharedCode=row["shared_code"], race=row["race"], _request_timeout=8)
                row["totalStock"] = max(0, int(data["count"]))
                row["stockUnknown"] = False
                cache[product_id] = (time.monotonic(), row)
                with self._snapshot_lock:
                    current = self._catalog.get(product_id)
                    if current is not None:
                        current.update(totalStock=row["totalStock"], stockUnknown=False)
            except (SupplierApiError, TypeError, ValueError, KeyError):
                row["stockUnknown"] = True
            return row
        ids = list(dict.fromkeys(product_ids))[:30]
        with ThreadPoolExecutor(max_workers=6) as executor:
            updated = list(executor.map(fetch, ids))
        with self._snapshot_lock:
            for row in updated:
                current = self._catalog.get(row["productId"])
                if current is not None and not row.get("stockUnknown", True):
                    current.update(totalStock=row["totalStock"], stockUnknown=False)
            self._save_snapshot()
        return updated

    def get_product_detail(self, product_id):
        try:
            return self._get_product_detail(product_id)
        except (TypeError, ValueError, KeyError, AttributeError, OverflowError, configparser.Error):
            raise SupplierApiError("Invalid Shared API inventory data") from None

    def _get_product_detail(self, product_id):
        catalog = getattr(self, "_catalog", None)
        if catalog is None or str(product_id) not in catalog:
            self.get_products()
            catalog = self._catalog
        for cached in catalog.values():
            row = dict(cached)
            if row["productId"] != str(product_id):
                continue
            if not self.account_product(row.get("categoryName"), row.get("productName")):
                raise SupplierApiError("该商品已隐藏")
            data = self._request("/shared/commodity/inventory", sharedCode=row["shared_code"], race=row["race"])
            if not isinstance(data, dict):
                raise SupplierApiError("Invalid Shared API inventory")
            if str(data.get("delivery_way", "")) != "0" or data.get("draft_status") in (1, "1"):
                row["purchaseBlocked"] = row.get("purchaseBlocked") or "该商品暂不支持自动下单"
            row["totalStock"] = max(0, int(data["count"]))
            row["stockUnknown"] = False
            config = self.parse_config(data.get("config"))
            price = (config.get("category_factory") or {}).get(row["race"]) if row["race"] else data.get("factory_price")
            try:
                cost = float(price)
            except (ValueError, TypeError):
                cost = 0
            if not math.isfinite(cost) or cost <= 0:
                cost = 0
                row["purchaseBlocked"] = "该商品成本价待确认，暂不可购买"
            row["priceUnknown"] = not bool(cost)
            row["price"] = cost * self._rate()
            with self._snapshot_lock:
                self._catalog[row["productId"]].update(row)
                self._save_snapshot()
            return {"success": True, "data": row}
        raise SupplierApiError("Shared API product unavailable")

    def buy_product(self, product_id, quantity, out_order_id):
        if quantity <= 0:
            raise SupplierApiError("Invalid quantity")
        row = self.get_product_detail(product_id)["data"]
        if row.get("purchaseBlocked"):
            raise SupplierApiError(row["purchaseBlocked"])
        minimum = max(1, int(row.get("minimum") or 1))
        maximum = max(0, int(row.get("maximum") or 0))
        if quantity < minimum or (maximum and quantity > maximum):
            raise SupplierApiError("Purchase quantity outside upstream limits")
        self._request("/shared/commodity/inventoryState", shared_code=row["shared_code"], race=row["race"], num=quantity, card_id=0)
        request_no = hashlib.sha256(str(out_order_id).encode()).hexdigest()[:32]
        task_id = f"ch5-{request_no}-{quantity}"
        trade = {}
        uncertain = False
        try:
            trade = self._request("/shared/commodity/trade", shared_code=row["shared_code"], race=row["race"],
                          contact="apibot-" + request_no, num=quantity, card_id=0, request_no=request_no)
        except SupplierApiError as exc:
            # This upstream uses code=0 for business rejections, not HTTP-style 4xx.
            # Only known pre-fulfillment rejections are safe to refund. Never retry a trade.
            if self.definitive_trade_rejection(exc):
                raise SupplierApiError(self.safe_order_error(exc)) from None
            uncertain = True
        trade = trade if isinstance(trade, dict) else {}
        result = self._order_payload(task_id, quantity, trade)
        result["data"]["submissionUncertain"] = uncertain or not bool(trade)
        return result

    @staticmethod
    def safe_order_error(exc):
        """Log fixed classifications only; upstream errors may contain credentials/card data."""
        payload = exc.payload if isinstance(exc.payload, dict) else {}
        message = str(payload.get("msg") or payload.get("message") or "")
        reasons = (
            ("订单不存在", "upstream order not found"),
            ("库存不足", "upstream stock insufficient"),
            ("余额不足", "upstream balance insufficient"),
            ("余额支付失败", "upstream balance payment rejected"),
            ("签名错误", "upstream signature rejected"),
            ("商品不存在", "upstream product unavailable"),
            ("商品已停售", "upstream product unavailable"),
            ("预选卡密不可用", "upstream preselection unavailable"),
        )
        return next((reason for text, reason in reasons if text in message),
                    "upstream response unavailable or unclassified; reconciliation required")

    @classmethod
    def definitive_trade_rejection(cls, exc):
        payload = exc.payload if isinstance(exc.payload, dict) else {}
        # A status code alone cannot prove that no money moved (e.g. rate limiting).
        return str(payload.get("code")) in {"0", "400"} and cls.safe_order_error(exc) in {
            "upstream stock insufficient", "upstream balance insufficient",
            "upstream balance payment rejected", "upstream signature rejected",
            "upstream product unavailable", "upstream preselection unavailable",
        }

    @staticmethod
    def _order_payload(task_id, quantity, data, trade_no=""):
        secret = data.get("secret")
        if secret and not isinstance(secret, str):
            secret = json.dumps(secret, ensure_ascii=False)
        complete = bool(secret and secret.strip())
        return {"success": True, "data": {"orderId": task_id, "status": "completed" if complete else "processing",
                "upstreamTradeNo": str(data.get("tradeNo") or trade_no or ""),
                "quantityRequested": quantity, "quantityDelivered": quantity if complete else 0, "amountRefunded": 0,
                "secret": secret or "", "files": [{"downloadUrl": "chuhai-order:" + task_id, "fileName": "delivery.txt"}] if complete else []}}

    def query_order(self, task_id, saved_payload=None):
        match = re.fullmatch(r"ch5-([a-f0-9]{32})-([1-9][0-9]*)", str(task_id))
        if not match:
            raise SupplierApiError("Invalid List 5 order ID")
        saved = (saved_payload or {}).get("data") or {}
        if saved and str(saved.get("orderId")) != str(task_id):
            raise SupplierApiError("List 5 saved order identity mismatch")
        trade_no = str(saved.get("upstreamTradeNo") or "")
        # The purchase response is durable in orders.raw_payload. Immediate delivery
        # must not depend on query2 being available, including after a process restart.
        if saved.get("secret"):
            return self._order_payload(task_id, int(match[2]), saved, trade_no)
        if trade_no:
            try:
                data = self._request("/shared/commodity/query", tradeNo=trade_no)
            except SupplierApiError:
                data = self._request("/shared/commodity/query2", requestNo=match[1])
        else:
            data = self._request("/shared/commodity/query2", requestNo=match[1])
        if not isinstance(data, dict):
            raise SupplierApiError("Invalid Shared API order")
        # Status enum is undocumented. Only actual delivery content proves completion.
        return self._order_payload(task_id, int(match[2]), data, trade_no)

    def download_secret(self, file_url, target, saved_payload=None):
        task_id = str(file_url).removeprefix("chuhai-order:")
        payload = self.query_order(task_id, saved_payload) if saved_payload is not None else self.query_order(task_id)
        secret = payload["data"]["secret"]
        if not secret:
            raise SupplierApiError("Delivery content not ready")
        target = Path(target)
        temp = target.with_suffix(target.suffix + ".part")
        temp.write_text(secret, encoding="utf-8")
        temp.replace(target)
        return target
