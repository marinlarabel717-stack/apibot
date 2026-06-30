from __future__ import annotations

import time
from dataclasses import dataclass, field
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
    session: requests.Session = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.session = requests.Session()

    def _base_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "User-Agent": "apibot/0.1",
            "Accept": "application/json",
        }
        headers.update(self.settings.api_extra_headers)
        return headers

    def _auth_header_variants(self) -> list[str | None]:
        header_name = self.settings.api_auth_header_name
        header_value = self.settings.api_auth_header_value
        if not header_name or not header_value:
            return [None]

        variants: list[str] = [header_value]
        should_try_both = (
            self.settings.api_auth_try_bearer_variants
            and header_name.lower() == "authorization"
        )
        if should_try_both:
            if header_value.lower().startswith("bearer "):
                raw_value = header_value[7:].strip()
                if raw_value:
                    variants.append(raw_value)
            else:
                variants.append(f"Bearer {header_value}")
        return list(dict.fromkeys(variants))

    def _headers(self, auth_value: str | None = None) -> dict[str, str]:
        headers = self._base_headers()
        if self.settings.api_auth_header_name and auth_value:
            headers[self.settings.api_auth_header_name] = auth_value
        return headers

    def _params(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        params.update(self.settings.api_extra_query)
        if self.settings.api_auth_query_name and self.settings.api_auth_query_value:
            params[self.settings.api_auth_query_name] = self.settings.api_auth_query_value
        if extra:
            params.update(extra)
        return params

    def _is_auth_failure(self, response: requests.Response, payload: Any | None) -> bool:
        if response.status_code in {401, 403}:
            return True
        if not isinstance(payload, dict):
            return False
        message = str(payload.get("msg") or payload.get("message") or "").strip().lower()
        return any(
            token in message
            for token in ("认证", "auth", "authorization", "unauthorized", "token", "请求错误", "request error")
        )

    def _parse_response(self, response: requests.Response) -> dict[str, Any]:
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise SupplierApiError("supplier returned a non-object JSON payload", payload)
        if payload.get("success") is not True or int(payload.get("code", 0) or 0) != 200:
            raise SupplierApiError(str(payload.get("msg") or "supplier request failed"), payload)
        return payload

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.settings.api_base_url}{path}"
        last_error: Exception | None = None

        for auth_value in self._auth_header_variants():
            response = self.session.get(
                url,
                params=self._params(params),
                headers=self._headers(auth_value),
                timeout=self.settings.api_timeout_seconds,
            )
            payload: Any | None = None
            try:
                return self._parse_response(response)
            except SupplierApiError as exc:
                payload = exc.payload
                last_error = exc
                if auth_value is not None and self._is_auth_failure(response, payload):
                    continue
                raise
            except requests.HTTPError as exc:
                last_error = exc
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
                if auth_value is not None and self._is_auth_failure(response, payload):
                    continue
                raise

        if last_error is not None:
            raise last_error
        raise SupplierApiError("supplier request failed")

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
