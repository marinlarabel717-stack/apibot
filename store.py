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
                    balance REAL NOT NULL DEFAULT 0,
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
                    raw_payload TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def ensure_user(self, user_id: int, username: str = "") -> None:
        with self._lock, self._connect() as conn:
            ts = now_iso()
            conn.execute(
                """
                INSERT INTO users (user_id, username, balance, created_at, updated_at)
                VALUES (?, ?, 0, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    updated_at = excluded.updated_at
                """,
                (int(user_id), username or "", ts, ts),
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
                INSERT INTO users (user_id, username, balance, created_at, updated_at)
                VALUES (?, '', 0, ?, ?)
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
                INSERT INTO users (user_id, username, balance, created_at, updated_at)
                VALUES (?, '', 0, ?, ?)
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
                    file_url, raw_payload, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, 0, 'processing', '', ?, ?, ?)
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

    def list_user_orders(self, user_id: int, limit: int = 10) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (int(user_id), int(limit)),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_processing_orders(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM orders WHERE state = 'processing' ORDER BY created_at ASC LIMIT ?",
                (int(limit),),
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

