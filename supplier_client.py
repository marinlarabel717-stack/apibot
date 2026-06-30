from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import requests

from config import Settings


class SupplierApiError(RuntimeError):
    def __init__(self, message: str, payload: Any | None = None) -> None:
        super().__init__(message)
        self.payload = payload


@dataclass(slots=True)
class SupplierClient:
    settings: Settings

    def __post_init__(self) -> None:
        self.session = requests.Session()

    def _headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": "apibot/0.1",
            "Accept": "application/json",
        }
        headers.update(self.settings.api_extra_headers)
        if self.settings.api_auth_header_name and self.settings.api_auth_header_value:
            headers[self.settings.api_auth_header_name] = self.settings.api_auth_header_value
        return headers

    def _params(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        params.update(self.settings.api_extra_query)
        if self.settings.api_auth_query_name and self.settings.api_auth_query_value:
            params[self.settings.api_auth_query_name] = self.settings.api_auth_query_value
        if extra:
            params.update(extra)
        return params

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.settings.api_base_url}{path}"
        response = self.session.get(
            url,
            params=self._params(params),
            headers=self._headers(),
            timeout=self.settings.api_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise SupplierApiError("供应商返回格式不是 JSON object", payload)
        if payload.get("success") is not True or int(payload.get("code", 0) or 0) != 200:
            raise SupplierApiError(str(payload.get("msg") or "供应商接口请求失败"), payload)
        return payload

    def get_categories(self) -> dict[str, Any]:
        return self._get("/tgapi/getCategoryList")

    def get_products(self, category_id: int) -> dict[str, Any]:
        return self._get("/tgapi/getProductListByCategoryId", {"categoryId": int(category_id)})

    def get_product_detail(self, product_id: int) -> dict[str, Any]:
        return self._get("/tgapi/getProductDetaiById", {"productId": int(product_id)})

    def search_products(self, text: str) -> dict[str, Any]:
        return self._get("/tgapi/searchProductListByText", {"text": text})

    def buy_product(self, product_id: int, quantity: int) -> dict[str, Any]:
        return self._get(
            "/tgapi/byTgAccountApi",
            {
                "productId": int(product_id),
                "quantityPurchased": int(quantity),
                "timestamp": int(time.time() * 1000),
            },
        )

    def query_order(self, task_id: str) -> dict[str, Any]:
        return self._get("/tgapi/queryOrderState", {"taskId": str(task_id)})

    def query_balance(self) -> dict[str, Any]:
        return self._get("/tgapi/queryBalance")

