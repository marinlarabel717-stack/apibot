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
            rows = [row for row in rows if self.platform(row.get("categoryName"))]
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
        return list(getattr(self, "_catalog", {}).values())

    def cached_product(self, product_id):
        row = getattr(self, "_catalog", {}).get(str(product_id))
        return dict(row) if row else None

    @property
    def configured(self):
        return bool(self.settings.chuhai_app_id and self.settings.chuhai_app_key)

    def _request(self, path, **params):
        if not self.configured:
            raise SupplierApiError("Account List 5: CHUHAI_APP_ID / CHUHAI_APP_KEY not configured")
        data = {"app_id": self.settings.chuhai_app_id, **params}
        data = {key: value for key, value in sorted(data.items()) if value != ""}
        # Match PHP md5(urldecode(http_build_query(params) . '&key=' . key)).
        signing = unquote_plus(urlencode(data) + "&key=" + self.settings.chuhai_app_key)
        data["sign"] = hashlib.md5(signing.encode("utf-8")).hexdigest()
        try:
            response = self.session.post(
                self.settings.chuhai_api_base_url + path, data=data,
                timeout=self.settings.chuhai_api_timeout_seconds, allow_redirects=False,
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
        selected = [category for category in categories if self.platform(category.get("name"))]
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
                data = self._request("/shared/commodity/inventory", sharedCode=row["shared_code"], race=row["race"])
                row["totalStock"] = max(0, int(data["count"]))
                row["stockUnknown"] = False
                cache[product_id] = (time.monotonic(), row)
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
            data = self._request("/shared/commodity/inventory", sharedCode=row["shared_code"], race=row["race"])
            if not isinstance(data, dict):
                raise SupplierApiError("Invalid Shared API inventory")
            if str(data.get("delivery_way", "")) != "0" or data.get("draft_status") in (1, "1"):
                row["purchaseBlocked"] = row.get("purchaseBlocked") or "该商品暂不支持自动下单"
            row["totalStock"] = max(0, int(data.get("count") or 0))
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
        try:
            self._request("/shared/commodity/trade", shared_code=row["shared_code"], race=row["race"],
                          contact="apibot-" + request_no, num=quantity, card_id=0, request_no=request_no)
        except SupplierApiError as exc:
            # A business rejection is definitive; transport/malformed responses require query2, never a second purchase/refund.
            if isinstance(exc.payload, dict) and str(exc.payload.get("code", "")).startswith("4") and len(str(exc.payload.get("code"))) == 3:
                raise
        return {"success": True, "data": {"orderId": task_id, "status": "processing"}}

    def query_order(self, task_id):
        match = re.fullmatch(r"ch5-([a-f0-9]{32})-([1-9][0-9]*)", str(task_id))
        if not match:
            raise SupplierApiError("Invalid List 5 order ID")
        data = self._request("/shared/commodity/query2", requestNo=match[1])
        if not isinstance(data, dict):
            raise SupplierApiError("Invalid Shared API order")
        secret = data.get("secret")
        if secret and not isinstance(secret, str):
            secret = json.dumps(secret, ensure_ascii=False)
        # Status enum is undocumented. Only actual delivery content proves completion.
        complete = bool(secret and secret.strip())
        quantity = int(match[2])
        return {"success": True, "data": {"orderId": task_id, "status": "completed" if complete else "processing",
                "quantityRequested": quantity, "quantityDelivered": quantity if complete else 0, "amountRefunded": 0,
                "secret": secret or "", "files": [{"downloadUrl": "chuhai-order:" + task_id, "fileName": "delivery.txt"}] if complete else []}}

    def download_secret(self, file_url, target):
        task_id = str(file_url).removeprefix("chuhai-order:")
        secret = self.query_order(task_id)["data"]["secret"]
        if not secret:
            raise SupplierApiError("Delivery content not ready")
        target = Path(target)
        temp = target.with_suffix(target.suffix + ".part")
        temp.write_text(secret, encoding="utf-8")
        temp.replace(target)
        return target
