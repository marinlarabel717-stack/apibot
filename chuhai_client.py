from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from urllib.parse import urlencode, unquote_plus

import requests
from supplier_client import SupplierApiError, SupplierClient


class ChuhaiSupplierClient(SupplierClient):
    """Shared API adapter. Local order IDs retain request_no + quantity for recovery."""

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
        return {"success": True, "data": {"accountBalance": data["balance"], "currency": "upstream", "userName": data.get("shopName", "")}}

    def _rate(self):
        value = self.settings.chuhai_price_rate
        if not math.isfinite(value) or value <= 0:
            raise SupplierApiError("Account List 5: configure CHUHAI_PRICE_RATE (upstream currency to local currency)")
        return value

    def get_products(self, query=""):
        try:
            return self._get_products(query)
        except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
            raise SupplierApiError("Invalid Shared API product data") from None

    def _get_products(self, query=""):
        rate = self._rate()
        data = self._request("/shared/commodity/items")
        if not isinstance(data, list):
            raise SupplierApiError("Invalid Shared API category array")
        rows = []
        for category in data:
            for item in category.get("children") or []:
                code = str(item.get("code") or item.get("shared_code") or "")
                # API widgets / preselected cards require additional user input; don't sell them silently.
                config = item.get("config") or {}
                if isinstance(config, str):
                    try:
                        config = json.loads(config)
                    except ValueError:
                        continue
                widget = item.get("widget") or []
                if isinstance(widget, str):
                    try:
                        widget = json.loads(widget)
                    except ValueError:
                        continue
                if not isinstance(config, dict) or widget or config.get("widget") or item.get("draft_status") in (1, "1"):
                    continue
                if str(item.get("delivery_way", "")) != "0":
                    continue
                if not code:
                    continue
                specs = config.get("category_factory") or {}
                if specs and not isinstance(specs, dict):
                    continue
                prices = specs.items() if specs else [("", item.get("factory_price"))]
                for race, price in prices:
                    try:
                        cost = float(price)
                    except (ValueError, TypeError):
                        continue
                    if not math.isfinite(cost) or cost <= 0:
                        continue
                    # Deterministic short callback ID; product codes/specs may contain colons or exceed Telegram's limit.
                    product_id = hashlib.sha256((code + "\0" + str(race)).encode()).hexdigest()[:20]
                    rows.append({"productId": product_id, "productName": str(item.get("name") or code) + (" / " + str(race) if race else ""),
                                 "categoryName": category.get("name", ""), "section": category.get("name", ""),
                                 "kind": "other", "price": cost * rate, "totalStock": max(0, int(item.get("inventory") or item.get("stock") or 0)),
                                 "source": "chuhai", "stockUnknown": True, "minimum": item.get("minimum"), "maximum": item.get("maximum"), "shared_code": code, "race": str(race)})
        self._catalog = {row["productId"]: row for row in rows}
        return {"success": True, "data": rows}

    def get_product_detail(self, product_id):
        try:
            return self._get_product_detail(product_id)
        except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
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
                raise SupplierApiError("Only automatic, non-preselected products are supported in List 5")
            row["totalStock"] = max(0, int(data.get("count") or 0))
            row["stockUnknown"] = False
            config = data.get("config") or {}
            if isinstance(config, str):
                config = json.loads(config)
            price = (config.get("category_factory") or {}).get(row["race"]) if row["race"] else data.get("factory_price")
            cost = float(price)
            if not math.isfinite(cost) or cost <= 0:
                raise SupplierApiError("Invalid Shared API cost")
            row["price"] = cost * self._rate()
            return {"success": True, "data": row}
        raise SupplierApiError("Shared API product unavailable")

    def buy_product(self, product_id, quantity, out_order_id):
        if quantity <= 0:
            raise SupplierApiError("Invalid quantity")
        row = self.get_product_detail(product_id)["data"]
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
