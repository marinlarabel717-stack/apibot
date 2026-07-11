from __future__ import annotations

import json
import random
import sqlite3
import threading
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
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

    @staticmethod
    def _format_trc20_amount(value: float) -> str:
        text = f"{float(value):.4f}"
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text

    @staticmethod
    def _quantize_recharge_amount(value: float) -> float:
        amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        return float(amount)

    @staticmethod
    def _is_future_or_equal(timestamp_text: str, now: datetime) -> bool:
        text = str(timestamp_text or "").strip()
        if not text:
            return False
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")) >= now
        except ValueError:
            return False

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    lang TEXT NOT NULL DEFAULT 'zh',
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
                    requested_amount REAL NOT NULL DEFAULT 0,
                    amount REAL NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'USDT',
                    state TEXT NOT NULL,
                    pay_address TEXT NOT NULL DEFAULT '',
                    pay_url TEXT NOT NULL DEFAULT '',
                    txid TEXT NOT NULL DEFAULT '',
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

                CREATE TABLE IF NOT EXISTS trc20_transfers (
                    txid TEXT PRIMARY KEY,
                    to_address TEXT NOT NULL DEFAULT '',
                    from_address TEXT NOT NULL DEFAULT '',
                    amount REAL NOT NULL DEFAULT 0,
                    currency TEXT NOT NULL DEFAULT 'USDT',
                    block_timestamp INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'seen',
                    matched_order_id TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
            if "display_name" not in user_columns:
                conn.execute("ALTER TABLE users ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")
            if "lang" not in user_columns:
                conn.execute("ALTER TABLE users ADD COLUMN lang TEXT NOT NULL DEFAULT 'zh'")
            if "is_active" not in user_columns:
                conn.execute("ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
            order_columns = {row["name"] for row in conn.execute("PRAGMA table_info(orders)").fetchall()}
            if "delivery_ready_sent_at" not in order_columns:
                conn.execute("ALTER TABLE orders ADD COLUMN delivery_ready_sent_at TEXT NOT NULL DEFAULT ''")
            if "delivery_sent_at" not in order_columns:
                conn.execute("ALTER TABLE orders ADD COLUMN delivery_sent_at TEXT NOT NULL DEFAULT ''")
            if "delivery_error" not in order_columns:
                conn.execute("ALTER TABLE orders ADD COLUMN delivery_error TEXT NOT NULL DEFAULT ''")
            topup_columns = {row["name"] for row in conn.execute("PRAGMA table_info(topup_orders)").fetchall()}
            if "requested_amount" not in topup_columns:
                conn.execute("ALTER TABLE topup_orders ADD COLUMN requested_amount REAL NOT NULL DEFAULT 0")
            if "pay_address" not in topup_columns:
                conn.execute("ALTER TABLE topup_orders ADD COLUMN pay_address TEXT NOT NULL DEFAULT ''")
            if "txid" not in topup_columns:
                conn.execute("ALTER TABLE topup_orders ADD COLUMN txid TEXT NOT NULL DEFAULT ''")
            conn.execute("UPDATE topup_orders SET requested_amount = amount WHERE requested_amount <= 0")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_topup_pending_channel ON topup_orders(state, channel, user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_topup_trc20_match ON topup_orders(channel, state, pay_address, amount)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trc20_transfer_state ON trc20_transfers(state, to_address, block_timestamp)")
            conn.commit()

    def ensure_user(self, user_id: int, username: str = "", display_name: str = "", lang: str = "") -> None:
        with self._lock, self._connect() as conn:
            ts = now_iso()
            conn.execute(
                """
                INSERT INTO users (user_id, username, display_name, lang, balance, is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, 0, 1, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    display_name = excluded.display_name,
                    lang = CASE
                        WHEN users.lang = '' AND excluded.lang != '' THEN excluded.lang
                        ELSE users.lang
                    END,
                    is_active = 1,
                    updated_at = excluded.updated_at
                """,
                (int(user_id), username or "", display_name or "", str(lang or "").strip(), ts, ts),
            )
            conn.commit()

    def get_user_lang(self, user_id: int, fallback: str = "zh") -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT lang FROM users WHERE user_id = ?", (int(user_id),)).fetchone()
            value = str((row["lang"] if row else fallback) or fallback).strip().lower()
            return value if value in {"zh", "en"} else fallback

    def set_user_lang(self, user_id: int, lang: str) -> None:
        normalized = str(lang or "zh").strip().lower()
        if normalized not in {"zh", "en"}:
            normalized = "zh"
        with self._lock, self._connect() as conn:
            ts = now_iso()
            conn.execute(
                """
                INSERT INTO users (user_id, username, display_name, lang, balance, is_active, created_at, updated_at)
                VALUES (?, '', '', ?, 0, 1, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    lang = excluded.lang,
                    updated_at = excluded.updated_at
                """,
                (int(user_id), normalized, ts, ts),
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
            debit_amount = float(amount)
            conn.execute(
                """
                INSERT INTO users (user_id, username, display_name, balance, is_active, created_at, updated_at)
                VALUES (?, '', '', 0, 1, ?, ?)
                ON CONFLICT(user_id) DO NOTHING
                """,
                (int(user_id), ts, ts),
            )
            if debit_amount <= 0:
                row = conn.execute("SELECT balance FROM users WHERE user_id = ?", (int(user_id),)).fetchone()
                balance = float(row["balance"]) if row else 0.0
                return False, balance
            cursor = conn.execute(
                "UPDATE users SET balance = balance - ?, updated_at = ? WHERE user_id = ? AND balance >= ?",
                (debit_amount, ts, int(user_id), debit_amount),
            )
            row = conn.execute("SELECT balance FROM users WHERE user_id = ?", (int(user_id),)).fetchone()
            balance = float(row["balance"]) if row else 0.0
            if cursor.rowcount <= 0:
                conn.commit()
                return False, balance
            conn.execute(
                """
                INSERT INTO wallet_ledger (user_id, amount, direction, reason, ref_id, note, created_at)
                VALUES (?, ?, 'debit', ?, ?, ?, ?)
                """,
                (int(user_id), debit_amount, reason, ref_id, note, ts),
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
                    ORDER BY
                        CASE WHEN balance > 0 THEN 0 ELSE 1 END ASC,
                        balance DESC,
                        created_at ASC,
                        user_id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (int(limit), int(offset)),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM users
                    ORDER BY
                        CASE WHEN balance > 0 THEN 0 ELSE 1 END ASC,
                        balance DESC,
                        created_at ASC,
                        user_id DESC
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
        requested_amount: float | None = None,
        pay_address: str = "",
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
                    order_id, user_id, channel, requested_amount, amount, currency, state, pay_address, pay_url,
                    txid, upstream_order_id, pay_user_id, callback_payload, note, message_id,
                    created_at, updated_at, expire_at, paid_at, canceled_at, cancel_reason
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, '', ?, '', '', ?, ?, ?, ?, ?, '', '', '')
                """,
                (
                    str(order_id),
                    int(user_id),
                    str(channel),
                    float(requested_amount if requested_amount is not None else amount),
                    float(amount),
                    str(currency or "USDT").upper(),
                    str(pay_address or ""),
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

    def create_trc20_topup_order(
        self,
        *,
        order_id: str,
        user_id: int,
        recharge_address: str,
        requested_amount: float,
        currency: str = "USDT",
        note: str = "trc20",
        expire_at: str = "",
    ) -> dict[str, Any]:
        recharge_address = str(recharge_address or "").strip()
        base = Decimal(str(self._quantize_recharge_amount(requested_amount)))
        if base <= 0:
            raise ValueError("充值金额必须大于 0")

        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            ts = now_iso()
            now_dt = datetime.now(timezone.utc)
            conn.execute(
                """
                UPDATE topup_orders
                SET state = 'canceled',
                    canceled_at = ?,
                    cancel_reason = 'recreated',
                    updated_at = ?
                WHERE user_id = ? AND channel = 'trc20' AND state = 'pending'
                """,
                (ts, ts, int(user_id)),
            )
            rows = conn.execute(
                """
                SELECT amount, state, expire_at
                FROM topup_orders
                WHERE channel = 'trc20' AND pay_address = ?
                """,
                (recharge_address,),
            ).fetchall()
            used = {
                self._format_trc20_amount(float(row["amount"]))
                for row in rows
                if str(row["state"] or "") in {"pending", "processing"}
                or self._is_future_or_equal(str(row["expire_at"] or ""), now_dt)
            }
            steps = list(range(1, 100))
            random.SystemRandom().shuffle(steps)
            pay_amount: float | None = None
            for step in steps:
                candidate = (base + (Decimal(step) / Decimal("10000"))).quantize(Decimal("0.0001"))
                text = self._format_trc20_amount(float(candidate))
                if text not in used:
                    pay_amount = float(candidate)
                    break
            if pay_amount is None:
                raise RuntimeError("当前 TRC20 待支付订单较多，请稍后再试")

            conn.execute(
                """
                INSERT INTO topup_orders (
                    order_id, user_id, channel, requested_amount, amount, currency, state, pay_address, pay_url,
                    txid, upstream_order_id, pay_user_id, callback_payload, note, message_id,
                    created_at, updated_at, expire_at, paid_at, canceled_at, cancel_reason
                ) VALUES (?, ?, 'trc20', ?, ?, ?, 'pending', ?, '', '', '', '', '', ?, 0, ?, ?, ?, '', '', '')
                """,
                (
                    str(order_id),
                    int(user_id),
                    float(base),
                    pay_amount,
                    str(currency or "USDT").upper(),
                    recharge_address,
                    str(note or "trc20"),
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

    def list_pending_topup_orders(
        self,
        user_id: int | None = None,
        channel: str = "",
        limit: int = 0,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT *
            FROM topup_orders
            WHERE state = 'pending'
        """
        params: list[Any] = []
        if user_id is not None:
            query += " AND user_id = ?"
            params.append(int(user_id))
        if channel:
            query += " AND channel = ?"
            params.append(str(channel))
        query += " ORDER BY created_at DESC"
        if limit > 0:
            query += " LIMIT ?"
            params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            return [dict(row) for row in rows]

    def list_pending_topup_amounts(self, channel: str, pay_address: str = "") -> list[float]:
        query = "SELECT amount FROM topup_orders WHERE channel = ? AND state = 'pending'"
        params: list[Any] = [str(channel)]
        if pay_address:
            query += " AND pay_address = ?"
            params.append(str(pay_address))
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            return [float(row["amount"]) for row in rows]

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
            txid = str((callback_payload or {}).get("txid") or "").strip()
            conn.execute(
                """
                UPDATE topup_orders
                SET state = 'processing',
                    txid = CASE WHEN ? != '' THEN ? ELSE txid END,
                    upstream_order_id = CASE WHEN ? != '' THEN ? ELSE upstream_order_id END,
                    pay_user_id = ?,
                    callback_payload = ?,
                    note = CASE WHEN ? != '' THEN ? ELSE note END,
                    updated_at = ?
                WHERE order_id = ? AND state = 'pending'
                """,
                (
                    txid,
                    txid,
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
                VALUES (?, ?, 'credit', ?, ?, ?, ?)
                """,
                (
                    int(order["user_id"]),
                    actual_amount,
                    f"{str(order.get('channel') or 'topup').lower()}_topup",
                    str(order_id),
                    str(note or paid_currency),
                    ts,
                ),
            )
            conn.execute(
                """
                UPDATE topup_orders
                SET state = 'paid',
                    paid_at = ?,
                    updated_at = ?,
                    txid = CASE WHEN ? != '' THEN ? ELSE txid END,
                    upstream_order_id = CASE WHEN ? != '' THEN ? ELSE upstream_order_id END,
                    pay_user_id = ?,
                    callback_payload = ?,
                    note = CASE WHEN ? != '' THEN ? ELSE note END
                WHERE order_id = ? AND state = 'processing'
                """,
                (
                    ts,
                    ts,
                    txid,
                    txid,
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

    def expire_topup_orders(self, channel: str = "") -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            now = now_iso()
            query = """
                SELECT *
                FROM topup_orders
                WHERE state = 'pending'
                  AND expire_at != ''
                  AND expire_at <= ?
            """
            params: list[Any] = [now]
            if channel:
                query += " AND channel = ?"
                params.append(str(channel))
            rows = conn.execute(query, tuple(params)).fetchall()
            if not rows:
                return []
            payloads = [dict(row) for row in rows]
            order_ids = [str(row["order_id"]) for row in payloads]
            placeholders = ",".join("?" for _ in order_ids)
            conn.execute(
                f"""
                UPDATE topup_orders
                SET state = 'expired',
                    canceled_at = ?,
                    cancel_reason = 'expired',
                    updated_at = ?
                WHERE order_id IN ({placeholders}) AND state = 'pending'
                """,
                (now, now, *order_ids),
            )
            conn.commit()
            return payloads

    def complete_trc20_topup(
        self,
        *,
        txid: str,
        to_address: str,
        from_address: str,
        paid_amount: float,
        currency: str = "USDT",
        block_timestamp: int = 0,
        payload: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any] | None]:
        txid = str(txid or "").strip()
        to_address = str(to_address or "").strip()
        from_address = str(from_address or "").strip()
        paid_amount = float(paid_amount or 0.0)
        paid_currency = str(currency or "USDT").upper()
        if not txid or not to_address or paid_amount <= 0:
            return "invalid_transfer", None
        event_type = str((payload or {}).get("event_type") or "").strip().lower()
        if any(keyword in event_type for keyword in ("approve", "approval", "authorize", "authorization")):
            return "ignored_event", None
        if event_type and "transfer" not in event_type:
            return "ignored_event", None

        with self._lock, self._connect() as conn:
            existing_tx = conn.execute("SELECT * FROM trc20_transfers WHERE txid = ?", (txid,)).fetchone()
            if existing_tx is not None:
                tx_state = str(existing_tx["state"] or "")
                matched_order_id = str(existing_tx["matched_order_id"] or "")
                if tx_state == "credited" and matched_order_id:
                    order_row = conn.execute("SELECT * FROM topup_orders WHERE order_id = ?", (matched_order_id,)).fetchone()
                    return "already_paid", dict(order_row) if order_row else None
                return tx_state or "duplicate", None

            ts = now_iso()
            payload_json = json.dumps(payload or {}, ensure_ascii=False)
            conn.execute(
                """
                INSERT INTO trc20_transfers (
                    txid, to_address, from_address, amount, currency, block_timestamp,
                    state, matched_order_id, payload, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'seen', '', ?, ?, ?)
                """,
                (
                    txid,
                    to_address,
                    from_address,
                    paid_amount,
                    paid_currency,
                    int(block_timestamp or 0),
                    payload_json,
                    ts,
                    ts,
                ),
            )

            transfer_time = ts
            if int(block_timestamp or 0) > 0:
                transfer_time = datetime.fromtimestamp(int(block_timestamp) / 1000, tz=timezone.utc).replace(microsecond=0).isoformat()

            row = conn.execute(
                """
                SELECT *
                FROM topup_orders
                WHERE channel = 'trc20'
                  AND state = 'pending'
                  AND currency = ?
                  AND pay_address = ?
                  AND ROUND(amount, 4) = ROUND(?, 4)
                  AND created_at <= ?
                  AND (expire_at = '' OR expire_at >= ?)
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (paid_currency, to_address, paid_amount, transfer_time, transfer_time),
            ).fetchone()
            if row is None:
                conn.execute(
                    "UPDATE trc20_transfers SET state = 'unmatched', updated_at = ? WHERE txid = ?",
                    (ts, txid),
                )
                conn.commit()
                return "unmatched", None

            order = dict(row)
            cur = conn.execute(
                """
                UPDATE topup_orders
                SET state = 'processing',
                    txid = ?,
                    pay_user_id = ?,
                    callback_payload = ?,
                    note = CASE WHEN note = '' THEN 'trc20' ELSE note END,
                    updated_at = ?
                WHERE order_id = ? AND state = 'pending'
                """,
                (txid, from_address, payload_json, ts, str(order["order_id"])),
            )
            if cur.rowcount != 1:
                fresh = conn.execute("SELECT * FROM topup_orders WHERE order_id = ?", (str(order["order_id"]),)).fetchone()
                conn.execute("UPDATE trc20_transfers SET updated_at = ? WHERE txid = ?", (ts, txid))
                conn.commit()
                return "processing", dict(fresh) if fresh else order

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
                (paid_amount, ts, int(order["user_id"])),
            )
            conn.execute(
                """
                INSERT INTO wallet_ledger (user_id, amount, direction, reason, ref_id, note, created_at)
                VALUES (?, ?, 'credit', 'trc20_topup', ?, ?, ?)
                """,
                (int(order["user_id"]), paid_amount, str(order["order_id"]), txid, ts),
            )
            conn.execute(
                """
                UPDATE topup_orders
                SET state = 'paid',
                    paid_at = ?,
                    updated_at = ?,
                    txid = ?,
                    pay_user_id = ?,
                    callback_payload = ?,
                    note = CASE WHEN note = '' THEN 'trc20' ELSE note END
                WHERE order_id = ? AND state = 'processing'
                """,
                (ts, ts, txid, from_address, payload_json, str(order["order_id"])),
            )
            conn.execute(
                """
                UPDATE trc20_transfers
                SET state = 'credited',
                    matched_order_id = ?,
                    updated_at = ?
                WHERE txid = ?
                """,
                (str(order["order_id"]), ts, txid),
            )
            conn.commit()
            fresh = conn.execute("SELECT * FROM topup_orders WHERE order_id = ?", (str(order["order_id"]),)).fetchone()
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
