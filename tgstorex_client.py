from __future__ import annotations

import json
import math
import re
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import requests

from supplier_client import SupplierApiError, SupplierClient


COUNTRY_METADATA = json.loads(Path(__file__).with_name("country_metadata.json").read_text(encoding="utf-8"))


class TgstorexPurchaseUncertain(SupplierApiError):
    pass


class TgstorexSupplierClient(SupplierClient):
    def __post_init__(self) -> None:
        super().__post_init__()
        self._rate_lock = threading.Lock()
        self._last_request = 0.0

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": "apibot/0.1",
            "Accept": "application/json",
            "Authorization": f"Bearer {self.settings.tgstorex_api_key}",
        }

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.settings.tgstorex_api_key:
            raise SupplierApiError("Account List 4 is not configured: TGSTOREX_API_KEY")
        with self._rate_lock:
            time.sleep(max(0.0, 0.55 - (time.monotonic() - self._last_request)))
            self._last_request = time.monotonic()
        purchase = method == "POST"
        try:
            response = self.session.request(
                method,
                f"{self.settings.tgstorex_api_base_url}{path}",
                params=params,
                json=json_body,
                headers=self._headers(),
                timeout=self.settings.tgstorex_api_timeout_seconds,
                allow_redirects=False,
            )
        except requests.RequestException:
            if purchase:
                raise TgstorexPurchaseUncertain("Purchase response unavailable; manual reconciliation required") from None
            raise SupplierApiError("Session supplier connection failed") from None
        if purchase and (response.status_code >= 500 or 300 <= response.status_code < 400 or response.status_code in {408, 409}):
            raise TgstorexPurchaseUncertain("Purchase outcome unknown; manual reconciliation required")
        if not 200 <= response.status_code < 300:
            raise SupplierApiError(f"Session supplier HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            error_type = TgstorexPurchaseUncertain if purchase else SupplierApiError
            raise error_type("Session supplier returned invalid JSON") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), (dict, list)):
            error_type = TgstorexPurchaseUncertain if purchase else SupplierApiError
            raise error_type("Session supplier returned invalid data")
        return payload

    def _normalize_product_row(
        self,
        row: dict[str, Any],
        *,
        shared_catalog: bool = False,
    ) -> dict[str, Any] | None:
        code = str(row.get("country_code") or row.get("code") or "").strip().upper()
        if len(code) != 2 or not code.isascii() or not code.isalpha():
            return None
        if row.get("is_active") in (False, 0, "0", "false"):
            return None
        row_type = str(row.get("type") or "sessions").strip().lower()
        allowed_types = {"sessions", "any", "number"} if shared_catalog else {"sessions", "any"}
        if row_type not in allowed_types:
            return None
        try:
            price = float(row["price"])
            stock = int(row["quantity"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(price) or price <= 0 or stock < 0:
            return None
        # ISO code is stable even when the upstream omits names and dial codes.
        metadata = COUNTRY_METADATA.get(code)
        country = metadata[0] if metadata else str(row.get("name") or code).strip()
        return {
            **row,
            "productId": code,
            "productName": country,
            "categoryName": "Sessions",
            "country": country,
            "country_en": metadata[1] if metadata else str(row.get("name") or code).strip(),
            "prefix": str(row.get("dial_code") or (metadata[2] if metadata else "")).strip(),
            "region": metadata[3] if metadata else "",
            "kind": "api_country",
            "totalStock": stock,
            "price": price,
            "source": "tgstorex",
        }

    def get_products(self, query: str = "") -> dict[str, Any]:
        payload = self._request_json("GET", "/prices", params={"type": "sessions"})
        rows = payload["data"]
        if not isinstance(rows, list):
            raise SupplierApiError("Session price list is not an array")
        meta = payload.get("meta")
        pricing_mode = str(meta.get("pricing_mode") or "").strip().lower() if isinstance(meta, dict) else ""
        shared_catalog = pricing_mode == "shared"
        products: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = self._normalize_product_row(row, shared_catalog=shared_catalog) if isinstance(row, dict) else None
            if item is None:
                continue
            code = item["productId"]
            if code in products:
                raise SupplierApiError(f"Ambiguous session price for country {code}")
            products[code] = item
        if rows and not products:
            number_rows = sum(
                1 for row in rows
                if isinstance(row, dict) and str(row.get("type") or "").strip().lower() == "number"
            )
            mode_label = "shared" if shared_catalog else "not confirmed shared"
            raise SupplierApiError(
                f"Upstream returned {len(rows)} price rows but no usable session countries "
                f"(pricing mode: {mode_label}; number rows: {number_rows}). "
                "Check the upstream session/shared pricing configuration."
            )
        return {"success": True, "data": list(products.values())}

    def get_product_detail(self, product_id: str) -> dict[str, Any]:
        code = str(product_id).strip().upper()
        for row in self.get_products()["data"]:
            if row["productId"] == code:
                return {"success": True, "data": row}
        raise SupplierApiError(f"Session country unavailable: {code}")

    def _normalize_order_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise SupplierApiError("Invalid session order")
        if data.get("order_type") not in (None, "sessions"):
            raise SupplierApiError("Not a session package order")
        raw_status = str(data.get("status") or "").lower()
        requested = max(0, int(data.get("quantity") or 0))
        delivered = max(0, int(data.get("completed_count") or 0))
        if delivered > requested:
            raise SupplierApiError("Invalid session order counts")
        terminal = raw_status in {"done", "completed", "partial", "partially_completed"}
        failed = raw_status in {"failed", "canceled", "cancelled", "refund", "refunded"}
        files = []
        for item in data.get("items") or []:
            if not isinstance(item, dict) or item.get("type") not in (None, "sessions"):
                continue
            file_url = str(item.get("file_url") or "").strip()
            if file_url:
                files.append({
                    "downloadUrl": self.resolve_download_url(file_url),
                    "fileName": PurePosixPath(str(item.get("file_path") or "sessions.zip")).name,
                })
        status = "processing"
        counts_ready = "completed_count" in data and requested > 0
        delivery_ready = (delivered > 0 and bool(files)) or (delivered == 0 and not files)
        if terminal and counts_ready and delivery_ready:
            status = "completed" if delivered == requested else "partial"
        elif failed and delivered == 0 and "completed_count" in data:
            status = "failed"
        elif failed and delivered > 0 and files:
            status = "partial"
        return {
            "success": True,
            "data": {
                "orderId": str(data.get("id") or data.get("order_id") or ""),
                "status": status,
                "quantityRequested": requested,
                "quantityDelivered": delivered,
                "quantityFailed": max(0, requested - delivered) if status != "processing" else 0,
                "amountRefunded": "0",
                "files": files,
                "upstream": data,
            },
        }

    def buy_product(self, product_id: str, quantity: int, out_order_id: str) -> dict[str, Any]:
        code = str(product_id).strip().upper()
        if len(code) != 2 or not code.isascii() or not code.isalpha() or quantity <= 0:
            raise SupplierApiError("Invalid session purchase")
        payload = self._request_json(
            "POST", "/orders/sessions/purchase",
            json_body={"country": code, "quantity": int(quantity)},
        )
        data = payload.get("data")
        if not isinstance(data, dict) or not str(data.get("id") or "").strip():
            raise TgstorexPurchaseUncertain("Purchase response has no order ID")
        return {"success": True, "data": {"orderId": str(data["id"]), "status": "processing"}}

    def query_order(self, task_id: str) -> dict[str, Any]:
        if str(task_id).startswith("tgx-review-"):
            return {"success": True, "data": {"orderId": task_id, "status": "processing", "needsReview": True}}
        payload = self._request_json("GET", f"/orders/{quote(str(task_id), safe='')}")
        try:
            return self._normalize_order_payload(payload)
        except (ValueError, TypeError, OverflowError):
            raise SupplierApiError("Invalid session order counts") from None

    def resolve_download_url(self, file_url: str) -> str:
        url = urljoin(f"{self.settings.tgstorex_api_base_url}/", str(file_url).strip())
        parsed = urlparse(url)
        origin = urlparse(self.settings.tgstorex_api_base_url)
        if parsed.scheme != "https" or parsed.netloc != origin.netloc or parsed.username or parsed.password:
            raise SupplierApiError("Untrusted session download URL")
        return url

    def build_download_headers(self) -> dict[str, str]:
        return {**self._headers(), "Accept": "application/zip, application/octet-stream"}

    def validate_delivery_file(self, path: Path) -> None:
        try:
            with zipfile.ZipFile(path) as archive:
                if not any(not item.is_dir() for item in archive.infolist()) or archive.testzip() is not None:
                    raise SupplierApiError("会话包为空或 ZIP 校验失败，请重试下载原订单，无需重新购买。")
        except (zipfile.BadZipFile, EOFError, RuntimeError, NotImplementedError, OSError) as exc:
            raise SupplierApiError("上游会话包不是完整可读的 ZIP，请重试下载原订单，无需重新购买。") from exc

    def download_session_file(self, file_url: str, target_path: Path) -> Path:
        if target_path.exists():
            try:
                self.validate_delivery_file(target_path)
                return target_path
            except SupplierApiError:
                pass  # Never reuse corrupt files cached by an older version.
        temp_path = target_path.with_suffix(target_path.suffix + ".part")
        download_url = self.resolve_download_url(file_url)
        expected_order = re.search(r"/orders/([^/]+)/file(?:/|$)", urlparse(download_url).path)
        visited = set()
        try:
            for _ in range(4):
                if download_url in visited:
                    raise SupplierApiError("上游下载地址循环跳转，请联系上游核查原订单。")
                visited.add(download_url)
                with self.session.get(
                    download_url,
                    headers=self.build_download_headers(),
                    timeout=self.settings.tgstorex_api_timeout_seconds,
                    stream=True,
                    allow_redirects=False,
                ) as response:
                    response.raise_for_status()
                    if 300 <= response.status_code < 400:
                        location = str(response.headers.get("Location") or "").strip()
                        if not location:
                            raise SupplierApiError("上游下载重定向缺少文件地址。")
                        download_url = self.resolve_download_url(urljoin(download_url, location))
                        continue
                    content_type = str(response.headers.get("Content-Type") or "").lower()
                    if "json" in content_type:
                        descriptor = bytearray()
                        for chunk in response.iter_content(chunk_size=8192):
                            descriptor.extend(chunk)
                            if len(descriptor) > 65536:
                                raise SupplierApiError("上游文件描述响应过大，已停止下载。")
                        try:
                            payload = json.loads(descriptor)
                        except (ValueError, UnicodeError):
                            raise SupplierApiError("上游文件描述不是有效 JSON。") from None
                        data = payload.get("data") if isinstance(payload, dict) else None
                        if not isinstance(data, dict) or data.get("ready") is not True:
                            raise SupplierApiError("上游会话包文件尚未就绪或下载权限不足，请稍后重试原订单。")
                        if expected_order and data.get("order_id") not in (None, expected_order.group(1)):
                            raise SupplierApiError("上游返回的会话包订单号不匹配，已停止下载。")
                        next_url = str(data.get("file_url") or "").strip()
                        if not next_url:
                            raise SupplierApiError("上游未提供实际会话包下载地址。")
                        next_url = self.resolve_download_url(urljoin(download_url, next_url))
                        parsed = urlparse(download_url)
                        if (
                            next_url == download_url
                            and expected_order
                            and re.fullmatch(r"/orders/[^/]+/file/?", parsed.path.removeprefix(urlparse(self.settings.tgstorex_api_base_url).path))
                            and not parsed.query
                        ):
                            # Some file routes return status until download mode is requested.
                            next_url = parsed._replace(query="download=1").geturl()
                        download_url = next_url
                        continue
                    if "html" in content_type:
                        raise SupplierApiError("上游下载返回了网页而不是 ZIP，请检查下载权限后重试原订单。")
                    with temp_path.open("wb") as handle:
                        for chunk in response.iter_content(chunk_size=65536):
                            if chunk:
                                handle.write(chunk)
                self.validate_delivery_file(temp_path)
                temp_path.replace(target_path)
                return target_path
            raise SupplierApiError("上游下载跳转次数过多，请联系上游核查原订单。")
        finally:
            temp_path.unlink(missing_ok=True)
