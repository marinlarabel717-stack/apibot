from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    balance REAL NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS wallet_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    amount REAL NOT NULL,
                    direction TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    ref_id TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS orders (
                    task_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    product_id INTEGER NOT NULL,
                    product_name TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    quantity_success INTEGER NOT NULL DEFAULT 0,
                    unit_price REAL NOT NULL,
                    total_price REAL NOT NULL,
                    refund_amount REAL NOT NULL DEFAULT 0,
                    state TEXT NOT NULL,
                    file_url TEXT NOT NULL DEFAULT '',
                    delivery_ready_sent_at TEXT NOT NULL DEFAULT '',
                    delivery_sent_at TEXT NOT NULL DEFAULT '',
                    delivery_error TEXT NOT NULL DEFAULT '',
                    raw_payload TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    updated_by INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS admin_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_user_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    target TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS topup_orders (
                    order_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    channel TEXT NOT NULL,
                    amount REAL NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'USDT',
                    state TEXT NOT NULL,
                    pay_url TEXT NOT NULL DEFAULT '',
                    upstream_order_id TEXT NOT NULL DEFAULT '',
                    pay_user_id TEXT NOT NULL DEFAULT '',
                    callback_payload TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    message_id INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expire_at TEXT NOT NULL DEFAULT '',
                    paid_at TEXT NOT NULL DEFAULT '',
                    canceled_at TEXT NOT NULL DEFAULT '',
                    cancel_reason TEXT NOT NULL DEFAULT ''
                );
                """
            )
            user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
            if "display_name" not in user_columns:
                conn.execute("ALTER TABLE users ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")
            if "is_active" not in user_columns:
                conn.execute("ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
            order_columns = {row["name"] for row in conn.execute("PRAGMA table_info(orders)").fetchall()}
            if "delivery_ready_sent_at" not in order_columns:
                conn.execute("ALTER TABLE orders ADD COLUMN delivery_ready_sent_at TEXT NOT NULL DEFAULT ''")
            if "delivery_sent_at" not in order_columns:
                conn.execute("ALTER TABLE orders ADD COLUMN delivery_sent_at TEXT NOT NULL DEFAULT ''")
            if "delivery_error" not in order_columns:
                conn.execute("ALTER TABLE orders ADD COLUMN delivery_error TEXT NOT NULL DEFAULT ''")
            conn.commit()

    def ensure_user(self, user_id: int, username: str = "", display_name: str = "") -> None:
        with self._lock, self._connect() as conn:
            ts = now_iso()
            conn.execute(
                """
                INSERT INTO users (user_id, username, display_name, balance, is_active, created_at, updated_at)
                VALUES (?, ?, ?, 0, 1, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    display_name = excluded.display_name,
                    is_active = 1,
                    updated_at = excluded.updated_at
                """,
                (int(user_id), username or "", display_name or "", ts, ts),
            )
            conn.commit()

    def get_balance(self, user_id: int) -> float:
        with self._connect() as conn:
            row = conn.execute("SELECT balance FROM users WHERE user_id = ?", (int(user_id),)).fetchone()
            return float(row["balance"]) if row else 0.0

    def add_balance(self, user_id: int, amount: float, reason: str, ref_id: str = "", note: str = "") -> float:
        with self._lock, self._connect() as conn:
            ts = now_iso()
            conn.execute(
                """
                INSERT INTO users (user_id, username, display_name, balance, is_active, created_at, updated_at)
                VALUES (?, '', '', 0, 1, ?, ?)
                ON CONFLICT(user_id) DO NOTHING
                """,
                (int(user_id), ts, ts),
            )
            conn.execute(
                "UPDATE users SET balance = balance + ?, updated_at = ? WHERE user_id = ?",
                (float(amount), ts, int(user_id)),
            )
            conn.execute(
                """
                INSERT INTO wallet_ledger (user_id, amount, direction, reason, ref_id, note, created_at)
                VALUES (?, ?, 'credit', ?, ?, ?, ?)
                """,
                (int(user_id), float(amount), reason, ref_id, note, ts),
            )
            balance = conn.execute("SELECT balance FROM users WHERE user_id = ?", (int(user_id),)).fetchone()["balance"]
            conn.commit()
            return float(balance)

    def debit_balance(self, user_id: int, amount: float, reason: str, ref_id: str = "", note: str = "") -> tuple[bool, float]:
        with self._lock, self._connect() as conn:
            ts = now_iso()
            conn.execute(
                """
                INSERT INTO users (user_id, username, display_name, balance, is_active, created_at, updated_at)
                VALUES (?, '', '', 0, 1, ?, ?)
                ON CONFLICT(user_id) DO NOTHING
                """,
                (int(user_id), ts, ts),
            )
            cur = conn.execute(
                """
                UPDATE users
                SET balance = balance - ?, updated_at = ?
                WHERE user_id = ? AND balance >= ?
                """,
                (float(amount), ts, int(user_id), float(amount)),
            )
            row = conn.execute("SELECT balance FROM users WHERE user_id = ?", (int(user_id),)).fetchone()
            balance = float(row["balance"]) if row else 0.0
            if cur.rowcount != 1:
                conn.rollback()
                return False, balance
            conn.execute(
                """
                INSERT INTO wallet_ledger (user_id, amount, direction, reason, ref_id, note, created_at)
                VALUES (?, ?, 'debit', ?, ?, ?, ?)
                """,
                (int(user_id), float(amount), reason, ref_id, note, ts),
            )
            conn.commit()
            return True, balance

    def record_order(
        self,
        task_id: str,
        user_id: int,
        username: str,
        product_id: int,
        product_name: str,
        quantity: int,
        unit_price: float,
        total_price: float,
        payload: dict[str, Any],
    ) -> None:
        with self._lock, self._connect() as conn:
            ts = now_iso()
            conn.execute(
                """
                INSERT OR REPLACE INTO orders (
                    task_id, user_id, username, product_id, product_name, quantity,
                    quantity_success, unit_price, total_price, refund_amount, state,
                    file_url, delivery_ready_sent_at, delivery_sent_at, delivery_error, raw_payload, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, 0, 'processing', '', '', '', '', ?, ?, ?)
                """,
                (
                    str(task_id),
                    int(user_id),
                    username or "",
                    int(product_id),
                    product_name,
                    int(quantity),
                    float(unit_price),
                    float(total_price),
                    json.dumps(payload, ensure_ascii=False),
                    ts,
                    ts,
                ),
            )
            conn.commit()

    def get_order(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM orders WHERE task_id = ?", (str(task_id),)).fetchone()
            return dict(row) if row else None

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (int(user_id),)).fetchone()
            return dict(row) if row else None

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        username = str(username or "").strip().lstrip("@")
        if not username:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE lower(username) = lower(?) ORDER BY updated_at DESC LIMIT 1",
                (username,),
            ).fetchone()
            return dict(row) if row else None

    def count_users(self, active_only: bool = True) -> int:
        with self._connect() as conn:
            if active_only:
                row = conn.execute("SELECT COUNT(*) AS total FROM users WHERE is_active = 1").fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) AS total FROM users").fetchone()
            return int(row["total"]) if row else 0

    def list_users(self, limit: int = 20, offset: int = 0, active_only: bool = True) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if active_only:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM users
                    WHERE is_active = 1
                    ORDER BY updated_at DESC, created_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (int(limit), int(offset)),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM users
                    ORDER BY updated_at DESC, created_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (int(limit), int(offset)),
                ).fetchall()
            return [dict(row) for row in rows]

    def mark_user_inactive(self, user_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE users SET is_active = 0, updated_at = ? WHERE user_id = ?",
                (now_iso(), int(user_id)),
            )
            conn.commit()

    def get_runtime_settings(self) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
            return {str(row["key"]): str(row["value"]) for row in rows}

    def set_runtime_setting(self, key: str, value: str, updated_by: int = 0) -> None:
        with self._lock, self._connect() as conn:
            ts = now_iso()
            conn.execute(
                """
                INSERT INTO app_settings (key, value, updated_at, updated_by)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at,
                    updated_by = excluded.updated_by
                """,
                (str(key), str(value), ts, int(updated_by)),
            )
            conn.commit()

    def log_admin_action(self, admin_user_id: int, action: str, target: str = "", detail: str = "") -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO admin_actions (admin_user_id, action, target, detail, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (int(admin_user_id), str(action), str(target), str(detail), now_iso()),
            )
            conn.commit()

    def cancel_pending_topup_orders(self, user_id: int, channel: str, reason: str = "recreated") -> int:
        with self._lock, self._connect() as conn:
            ts = now_iso()
            cur = conn.execute(
                """
                UPDATE topup_orders
                SET state = 'canceled',
                    canceled_at = ?,
                    cancel_reason = ?,
                    updated_at = ?
                WHERE user_id = ? AND channel = ? AND state = 'pending'
                """,
                (ts, str(reason), ts, int(user_id), str(channel)),
            )
            conn.commit()
            return int(cur.rowcount or 0)

    def create_topup_order(
        self,
        order_id: str,
        user_id: int,
        channel: str,
        amount: float,
        currency: str = "USDT",
        *,
        pay_url: str = "",
        upstream_order_id: str = "",
        note: str = "",
        message_id: int = 0,
        expire_at: str = "",
    ) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            ts = now_iso()
            conn.execute(
                """
                INSERT INTO topup_orders (
                    order_id, user_id, channel, amount, currency, state, pay_url,
                    upstream_order_id, pay_user_id, callback_payload, note, message_id,
                    created_at, updated_at, expire_at, paid_at, canceled_at, cancel_reason
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, '', '', ?, ?, ?, ?, ?, '', '', '')
                """,
                (
                    str(order_id),
                    int(user_id),
                    str(channel),
                    float(amount),
                    str(currency or "USDT").upper(),
                    str(pay_url or ""),
                    str(upstream_order_id or ""),
                    str(note or ""),
                    int(message_id),
                    ts,
                    ts,
                    str(expire_at or ""),
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM topup_orders WHERE order_id = ?", (str(order_id),)).fetchone()
            return dict(row) if row else {}

    def set_topup_order_message_id(self, order_id: str, message_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE topup_orders SET message_id = ?, updated_at = ? WHERE order_id = ?",
                (int(message_id), now_iso(), str(order_id)),
            )
            conn.commit()

    def get_topup_order(self, order_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM topup_orders WHERE order_id = ?", (str(order_id),)).fetchone()
            return dict(row) if row else None

    def get_latest_pending_topup_order(self, user_id: int, channel: str = "") -> dict[str, Any] | None:
        query = "SELECT * FROM topup_orders WHERE user_id = ? AND state = 'pending'"
        params: list[Any] = [int(user_id)]
        if channel:
            query += " AND channel = ?"
            params.append(str(channel))
        query += " ORDER BY created_at DESC LIMIT 1"
        with self._connect() as conn:
            row = conn.execute(query, tuple(params)).fetchone()
            return dict(row) if row else None

    def cancel_topup_order(self, order_id: str, *, user_id: int | None = None, reason: str = "canceled") -> tuple[bool, dict[str, Any] | None]:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM topup_orders WHERE order_id = ?", (str(order_id),)).fetchone()
            if row is None:
                return False, None
            payload = dict(row)
            if user_id is not None and int(payload.get("user_id") or 0) != int(user_id):
                return False, payload
            if payload.get("state") != "pending":
                return False, payload
            ts = now_iso()
            conn.execute(
                """
                UPDATE topup_orders
                SET state = 'canceled', canceled_at = ?, cancel_reason = ?, updated_at = ?
                WHERE order_id = ? AND state = 'pending'
                """,
                (ts, str(reason), ts, str(order_id)),
            )
            conn.commit()
            fresh = conn.execute("SELECT * FROM topup_orders WHERE order_id = ?", (str(order_id),)).fetchone()
            return True, dict(fresh) if fresh else payload

    def complete_topup_order(
        self,
        order_id: str,
        *,
        paid_amount: float,
        currency: str = "USDT",
        upstream_order_id: str = "",
        pay_user_id: str = "",
        callback_payload: dict[str, Any] | None = None,
        note: str = "",
    ) -> tuple[str, dict[str, Any] | None]:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM topup_orders WHERE order_id = ?", (str(order_id),)).fetchone()
            if row is None:
                return "order_not_found", None
            order = dict(row)
            state = str(order.get("state") or "")
            if state == "paid":
                return "already_paid", order
            if state == "processing":
                return "processing", order
            if state != "pending":
                return state or "invalid_state", order

            expire_at = str(order.get("expire_at") or "").strip()
            if expire_at:
                try:
                    if datetime.fromisoformat(expire_at.replace("Z", "+00:00")) < datetime.now(timezone.utc):
                        ts = now_iso()
                        conn.execute(
                            """
                            UPDATE topup_orders
                            SET state = 'expired', canceled_at = ?, cancel_reason = 'expired', updated_at = ?
                            WHERE order_id = ? AND state = 'pending'
                            """,
                            (ts, ts, str(order_id)),
                        )
                        conn.commit()
                        fresh = conn.execute("SELECT * FROM topup_orders WHERE order_id = ?", (str(order_id),)).fetchone()
                        return "expired", dict(fresh) if fresh else order
                except ValueError:
                    pass

            expected_currency = str(order.get("currency") or "USDT").upper()
            paid_currency = str(currency or expected_currency).upper()
            if paid_currency != expected_currency:
                return "coin_mismatch", order

            expected_amount = float(order.get("amount") or 0.0)
            actual_amount = float(paid_amount)
            if expected_amount <= 0 or actual_amount <= 0:
                return "invalid_amount", order
            if round(expected_amount, 2) != round(actual_amount, 2):
                return "amount_mismatch", order

            ts = now_iso()
            callback_json = json.dumps(callback_payload or {}, ensure_ascii=False)
            conn.execute(
                """
                UPDATE topup_orders
                SET state = 'processing',
                    upstream_order_id = CASE WHEN ? != '' THEN ? ELSE upstream_order_id END,
                    pay_user_id = ?,
                    callback_payload = ?,
                    note = CASE WHEN ? != '' THEN ? ELSE note END,
                    updated_at = ?
                WHERE order_id = ? AND state = 'pending'
                """,
                (
                    str(upstream_order_id or ""),
                    str(upstream_order_id or ""),
                    str(pay_user_id or ""),
                    callback_json,
                    str(note or ""),
                    str(note or ""),
                    ts,
                    str(order_id),
                ),
            )
            conn.execute(
                """
                INSERT INTO users (user_id, username, display_name, balance, is_active, created_at, updated_at)
                VALUES (?, '', '', 0, 1, ?, ?)
                ON CONFLICT(user_id) DO NOTHING
                """,
                (int(order["user_id"]), ts, ts),
            )
            conn.execute(
                "UPDATE users SET balance = balance + ?, updated_at = ? WHERE user_id = ?",
                (actual_amount, ts, int(order["user_id"])),
            )
            conn.execute(
                """
                INSERT INTO wallet_ledger (user_id, amount, direction, reason, ref_id, note, created_at)
                VALUES (?, ?, 'credit', 'okpay_topup', ?, ?, ?)
                """,
                (int(order["user_id"]), actual_amount, str(order_id), str(note or paid_currency), ts),
            )
            conn.execute(
                """
                UPDATE topup_orders
                SET state = 'paid',
                    paid_at = ?,
                    updated_at = ?,
                    upstream_order_id = CASE WHEN ? != '' THEN ? ELSE upstream_order_id END,
                    pay_user_id = ?,
                    callback_payload = ?,
                    note = CASE WHEN ? != '' THEN ? ELSE note END
                WHERE order_id = ? AND state = 'processing'
                """,
                (
                    ts,
                    ts,
                    str(upstream_order_id or ""),
                    str(upstream_order_id or ""),
                    str(pay_user_id or ""),
                    callback_json,
                    str(note or ""),
                    str(note or ""),
                    str(order_id),
                ),
            )
            conn.commit()
            fresh = conn.execute("SELECT * FROM topup_orders WHERE order_id = ?", (str(order_id),)).fetchone()
            return "paid", dict(fresh) if fresh else order

    def list_user_orders(self, user_id: int, limit: int = 10) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (int(user_id), int(limit)),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_user_summary(self, user_id: int) -> dict[str, float]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN state != 'failed' THEN total_price - refund_amount ELSE 0 END), 0) AS total_spent,
                    COALESCE(SUM(CASE WHEN state != 'failed' THEN quantity ELSE 0 END), 0) AS total_quantity
                FROM orders
                WHERE user_id = ?
                """,
                (int(user_id),),
            ).fetchone()
            return {
                "total_spent": float(row["total_spent"]) if row else 0.0,
                "total_quantity": float(row["total_quantity"]) if row else 0.0,
            }

    def list_processing_orders(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM orders WHERE state = 'processing' ORDER BY created_at ASC LIMIT ?",
                (int(limit),),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_pending_delivery_orders(self, limit: int = 100, retry_cooldown_seconds: int = 60) -> list[dict[str, Any]]:
        with self._connect() as conn:
            cutoff = datetime.now(timezone.utc).timestamp() - max(0, int(retry_cooldown_seconds))
            rows = conn.execute(
                """
                SELECT *
                FROM orders
                WHERE state IN ('completed', 'partial')
                  AND file_url != ''
                  AND delivery_sent_at = ''
                  AND (
                        delivery_error = ''
                        OR strftime('%s', updated_at) <= ?
                  )
                ORDER BY updated_at ASC
                LIMIT ?
                """,
                (int(cutoff), int(limit)),
            ).fetchall()
            return [dict(row) for row in rows]

    def finalize_order(
        self,
        task_id: str,
        new_state: str,
        quantity_success: int,
        file_url: str,
        refund_amount: float,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, bool]:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM orders WHERE task_id = ?", (str(task_id),)).fetchone()
            if row is None:
                return None, False
            if row["state"] != "processing":
                return dict(row), False

            ts = now_iso()
            if refund_amount > 0:
                conn.execute(
                    "UPDATE users SET balance = balance + ?, updated_at = ? WHERE user_id = ?",
                    (float(refund_amount), ts, int(row["user_id"])),
                )
                conn.execute(
                    """
                    INSERT INTO wallet_ledger (user_id, amount, direction, reason, ref_id, note, created_at)
                    VALUES (?, ?, 'credit', 'order_refund', ?, ?, ?)
                    """,
                    (int(row["user_id"]), float(refund_amount), str(task_id), new_state, ts),
                )

            conn.execute(
                """
                UPDATE orders
                SET state = ?, quantity_success = ?, file_url = ?, refund_amount = ?, raw_payload = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (
                    new_state,
                    int(quantity_success),
                    file_url or "",
                    float(refund_amount),
                    json.dumps(payload, ensure_ascii=False),
                    ts,
                    str(task_id),
                ),
            )
            conn.commit()
            fresh = conn.execute("SELECT * FROM orders WHERE task_id = ?", (str(task_id),)).fetchone()
            return dict(fresh) if fresh else None, True

    def mark_order_delivery_ready_sent(self, task_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            ts = now_iso()
            conn.execute(
                """
                UPDATE orders
                SET delivery_ready_sent_at = CASE
                        WHEN delivery_ready_sent_at = '' THEN ?
                        ELSE delivery_ready_sent_at
                    END,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (ts, ts, str(task_id)),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM orders WHERE task_id = ?", (str(task_id),)).fetchone()
            return dict(row) if row else None

    def mark_order_delivery_sent(self, task_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            ts = now_iso()
            conn.execute(
                """
                UPDATE orders
                SET delivery_sent_at = ?, delivery_error = '', updated_at = ?
                WHERE task_id = ?
                """,
                (ts, ts, str(task_id)),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM orders WHERE task_id = ?", (str(task_id),)).fetchone()
            return dict(row) if row else None

    def mark_order_delivery_failed(self, task_id: str, error: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            ts = now_iso()
            conn.execute(
                """
                UPDATE orders
                SET delivery_error = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (str(error or "")[:1000], ts, str(task_id)),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM orders WHERE task_id = ?", (str(task_id),)).fetchone()
            return dict(row) if row else None

    def update_order_delivery_file(
        self,
        task_id: str,
        file_url: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            ts = now_iso()
            if payload is None:
                conn.execute(
                    """
                    UPDATE orders
                    SET file_url = ?, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (str(file_url or ""), ts, str(task_id)),
                )
            else:
                conn.execute(
                    """
                    UPDATE orders
                    SET file_url = ?, raw_payload = ?, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (
                        str(file_url or ""),
                        json.dumps(payload, ensure_ascii=False),
                        ts,
                        str(task_id),
                    ),
                )
            conn.commit()
            row = conn.execute("SELECT * FROM orders WHERE task_id = ?", (str(task_id),)).fetchone()
            return dict(row) if row else None
