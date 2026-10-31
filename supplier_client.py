from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

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
        self.session = self._build_session()

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=1,
            connect=1,
            read=1,
            backoff_factor=0.2,
            allowed_methods=frozenset({"GET"}),
            status_forcelist=(502, 503, 504),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(pool_connections=8, pool_maxsize=16, max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

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
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            payload: Any | None = None
            try:
                payload = response.json()
            except ValueError:
                payload = None
            message = self._build_http_error_message(response, payload)
            raise SupplierApiError(message, payload) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise SupplierApiError("supplier returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise SupplierApiError("supplier returned a non-object JSON payload", payload)
        if payload.get("success") is not True or int(payload.get("code", 0) or 0) != 200:
            raise SupplierApiError(str(payload.get("msg") or "supplier request failed"), payload)
        return payload

    def _build_http_error_message(self, response: requests.Response, payload: Any | None) -> str:
        if isinstance(payload, dict):
            remote_message = str(payload.get("msg") or payload.get("message") or "").strip()
            if remote_message:
                return remote_message

        reason = str(getattr(response, "reason", "") or "").strip()
        if reason:
            return f"HTTP {response.status_code} {reason}"
        return f"HTTP {response.status_code}"

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.settings.api_base_url}{path}"
        last_error: Exception | None = None

        for auth_value in self._auth_header_variants():
            try:
                response = self.session.get(
                    url,
                    params=self._params(params),
                    headers=self._headers(auth_value),
                    timeout=self.settings.api_timeout_seconds,
                )
                payload: Any | None = None
                return self._parse_response(response)
            except SupplierApiError as exc:
                payload = exc.payload
                last_error = exc
                if auth_value is not None and self._is_auth_failure(response, payload):
                    continue
                raise
            except requests.RequestException as exc:
                # Idle upstream keep-alive sockets occasionally go stale; rebuild once and retry.
                self.session.close()
                self.session = self._build_session()
                try:
                    response = self.session.get(
                        url,
                        params=self._params(params),
                        headers=self._headers(auth_value),
                        timeout=self.settings.api_timeout_seconds,
                    )
                    return self._parse_response(response)
                except SupplierApiError as retry_exc:
                    last_error = retry_exc
                    raise retry_exc
                except requests.RequestException as retry_request_exc:
                    last_error = SupplierApiError(str(retry_request_exc))
                    raise last_error from retry_request_exc

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
        return self._get(
            "/tgapi/queryOrderState",
            {
                "taskId": str(task_id),
                "timestamp": int(time.time() * 1000),
            },
        )

    def query_balance(self) -> dict[str, Any]:
        return self._get("/tgapi/queryBalance")

    def resolve_download_url(self, file_url: str) -> str:
        return str(file_url or "").strip()

    def build_download_headers(self) -> dict[str, str]:
        return {}


@dataclass(slots=True)
class DaxiSupplierClient(SupplierClient):
    def _headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": "apibot/0.1",
            "Accept": "application/json",
        }
        api_key = str(self.settings.daxi_api_key or "").strip()
        if api_key:
            headers["X-API-Key"] = api_key
        return headers

    def _parse_response(self, response: requests.Response) -> dict[str, Any]:
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            payload: Any | None = None
            try:
                payload = response.json()
            except ValueError:
                payload = None
            raise SupplierApiError(self._build_http_error_message(response, payload), payload) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise SupplierApiError("supplier returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise SupplierApiError("supplier returned a non-object JSON payload", payload)
        if payload.get("success") is not True:
            raise SupplierApiError(str(payload.get("message") or payload.get("msg") or "supplier request failed"), payload)
        return payload

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.settings.daxi_api_base_url}{path}"
        headers = self._headers()
        timeout = self.settings.daxi_api_timeout_seconds
        try:
            response = self.session.request(
                method=method.upper(),
                url=url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=timeout,
            )
            return self._parse_response(response)
        except requests.RequestException as exc:
            self.session.close()
            self.session = self._build_session()
            try:
                response = self.session.request(
                    method=method.upper(),
                    url=url,
                    params=params,
                    json=json_body,
                    headers=headers,
                    timeout=timeout,
                )
                return self._parse_response(response)
            except SupplierApiError:
                raise
            except requests.RequestException as retry_exc:
                raise SupplierApiError(str(retry_exc)) from retry_exc

    def get_products(self, query: str = "") -> dict[str, Any]:
        params = {"query": str(query).strip()} if str(query).strip() else None
        return self._request_json("GET", "/api/v1/products", params=params)

    def get_product_detail(self, product_id: str) -> dict[str, Any]:
        payload = self.get_products()
        product_id_text = str(product_id or "").strip()
        for row in payload.get("data") or []:
            if str(row.get("productId") or "").strip() == product_id_text:
                return {"success": True, "data": row}
        raise SupplierApiError(f"product not found: {product_id_text}", payload)

    def search_products(self, text: str) -> dict[str, Any]:
        return self.get_products(text)

    def buy_product(self, product_id: str, quantity: int, out_order_id: str) -> dict[str, Any]:
        return self._request_json(
            "POST",
            "/api/v1/orders",
            json_body={
                "productId": str(product_id).strip(),
                "quantityPurchased": int(quantity),
                "outOrderId": str(out_order_id).strip(),
            },
        )

    def query_order(self, task_id: str) -> dict[str, Any]:
        return self._request_json("GET", f"/api/v1/orders/{str(task_id).strip()}")

    def query_balance(self) -> dict[str, Any]:
        payload = self._request_json("GET", "/api/v1/balance")
        data = payload.get("data")
        if not isinstance(data, dict):
            data = payload if isinstance(payload, dict) else {}
        profile = data.get("user") if isinstance(data.get("user"), dict) else {}
        return {
            "success": True,
            "data": {
                "userId": (
                    data.get("userId")
                    or data.get("user_id")
                    or data.get("uid")
                    or data.get("id")
                    or profile.get("userId")
                    or profile.get("user_id")
                    or profile.get("uid")
                    or profile.get("id")
                    or data.get("telegramId")
                    or data.get("telegram_id")
                ),
                "userName": (
                    data.get("userName")
                    or data.get("username")
                    or data.get("user_name")
                    or data.get("name")
                    or profile.get("userName")
                    or profile.get("username")
                    or profile.get("user_name")
                    or profile.get("name")
                ),
                "accountBalance": (
                    data.get("accountBalance")
                    or data.get("balance")
                    or data.get("usdt")
                    or data.get("amount")
                    or profile.get("accountBalance")
                    or profile.get("balance")
                    or profile.get("usdt")
                    or profile.get("amount")
                ),
            },
        }

    def resolve_download_url(self, file_url: str) -> str:
        return urljoin(f"{self.settings.daxi_api_base_url}/", str(file_url or "").strip())

    def build_download_headers(self) -> dict[str, str]:
        headers = self._headers()
        headers["Accept"] = "*/*"
        return headers


@dataclass(slots=True)
class RzSupplierClient(SupplierClient):
    def _headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": "apibot/0.1",
            "Accept": "application/json",
        }
        api_key = str(self.settings.rz_api_key or "").strip()
        if api_key:
            headers["X-Api-Key"] = api_key
        return headers

    def _parse_response(self, response: requests.Response) -> dict[str, Any]:
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            payload: Any | None = None
            try:
                payload = response.json()
            except ValueError:
                payload = None
            raise SupplierApiError(self._build_http_error_message(response, payload), payload) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise SupplierApiError("supplier returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise SupplierApiError("supplier returned a non-object JSON payload", payload)
        if payload.get("error"):
            error = payload.get("error") or {}
            raise SupplierApiError(str(error.get("message") or "supplier request failed"), payload)
        return payload

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.settings.rz_api_base_url}{path}"
        headers = self._headers()
        timeout = self.settings.rz_api_timeout_seconds
        try:
            response = self.session.request(
                method=method.upper(),
                url=url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=timeout,
            )
            return self._parse_response(response)
        except requests.RequestException as exc:
            self.session.close()
            self.session = self._build_session()
            try:
                response = self.session.request(
                    method=method.upper(),
                    url=url,
                    params=params,
                    json=json_body,
                    headers=headers,
                    timeout=timeout,
                )
                return self._parse_response(response)
            except SupplierApiError:
                raise
            except requests.RequestException as retry_exc:
                raise SupplierApiError(str(retry_exc)) from retry_exc

    def _normalize_product_row(self, row: dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item["productId"] = str(item.get("id") or item.get("productId") or "").strip()
        country_name = str(item.get("country_name") or item.get("countryName") or "").strip()
        kind = str(item.get("kind") or "").strip()
        section = str(item.get("section") or "").strip()
        phone_code = str(item.get("phone_code") or item.get("phoneCode") or "").strip()
        base_name = country_name or section or kind or item["productId"] or "商品"
        item["productName"] = str(item.get("productName") or base_name).strip()
        item["category"] = kind
        item["categoryName"] = section or kind or "账号列表3"
        item["country"] = country_name
        item["prefix"] = phone_code
        item["totalStock"] = int(item.get("stock") or 0)
        try:
            item["price"] = float(item.get("price") or 0)
        except (TypeError, ValueError):
            item["price"] = 0.0
        return item

    def _normalize_order_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        status_raw = str(payload.get("status") or "").strip().lower()
        status_map = {
            "processing": "processing",
            "completed": "completed",
            "partially_completed": "partial",
            "partial": "partial",
            "failed": "failed",
        }
        normalized_status = status_map.get(status_raw, status_raw or "processing")
        delivered = max(0, int(payload.get("delivered") or 0))
        requested = max(delivered, int(payload.get("requested") or 0))
        failed = max(0, int(payload.get("failed") or 0))
        download = payload.get("download") or {}
        files: list[dict[str, str]] = []
        if isinstance(download, dict):
            download_url = str(download.get("url") or "").strip()
            token = str(download.get("token") or "").strip()
            if not download_url and token:
                download_url = f"/v1/downloads/{token}"
            if download_url:
                files.append(
                    {
                        "downloadUrl": download_url,
                        "fileName": str(download.get("filename") or "").strip(),
                    }
                )
        return {
            "success": True,
            "data": {
                "orderId": str(payload.get("order_id") or "").strip(),
                "status": normalized_status,
                "quantityRequested": requested,
                "quantityDelivered": delivered,
                "quantityFailed": failed,
                "amountRefunded": "0",
                "ready": bool(payload.get("ready")),
                "files": files,
            },
        }

    def get_products(self, query: str = "") -> dict[str, Any]:
        limit = 100
        offset = 0
        products: list[dict[str, Any]] = []
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        query_text = str(query or "").strip()
        if query_text:
            params["q"] = query_text

        while True:
            params["offset"] = offset
            payload = self._request_json("GET", "/v1/products", params=params)
            rows = payload.get("products") or []
            products.extend(self._normalize_product_row(row) for row in rows if isinstance(row, dict))
            count = int(payload.get("count") or len(products))
            if len(rows) < limit or len(products) >= count:
                break
            offset += limit

        return {"success": True, "data": products}

    def get_product_detail(self, product_id: str) -> dict[str, Any]:
        payload = self._request_json("GET", f"/v1/products/{quote(str(product_id or '').strip(), safe='')}")
        return {"success": True, "data": self._normalize_product_row(payload)}

    def search_products(self, text: str) -> dict[str, Any]:
        return self.get_products(text)

    def buy_product(self, product_id: str, quantity: int, out_order_id: str) -> dict[str, Any]:
        payload = self._request_json(
            "POST",
            "/v1/buy",
            json_body={
                "product_id": str(product_id).strip(),
                "quantity": int(quantity),
            },
        )
        return self._normalize_order_payload(payload)

    def query_order(self, task_id: str) -> dict[str, Any]:
        payload = self._request_json("GET", f"/v1/orders/{str(task_id).strip()}")
        return self._normalize_order_payload(payload)

    def query_balance(self) -> dict[str, Any]:
        payload = self._request_json("GET", "/v1/me")
        return {
            "success": True,
            "data": {
                "userId": payload.get("telegram_id"),
                "userName": payload.get("username"),
                "accountBalance": payload.get("balance"),
            },
        }

    def resolve_download_url(self, file_url: str) -> str:
        return urljoin(f"{self.settings.rz_api_base_url}/", str(file_url or "").strip())

    def build_download_headers(self) -> dict[str, str]:
        headers = self._headers()
        headers["Accept"] = "*/*"
        return headers
