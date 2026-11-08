import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from time import time

MOSCOW = timezone(timedelta(hours=3))
TRIAL_SECONDS = 15 * 60
PLANS = {1: "1.5", 3: "4", 7: "10"}


def access_db_path(root):
    return root / ("access-testnet.sqlite3" if os.getenv("CRYPTO_PAY_TESTNET", "0") == "1" else "access.sqlite3")


def matches_order(invoice: dict, token: str, amount: str) -> bool:
    if invoice.get("asset") != "TON" or invoice.get("payload") != token:
        return False
    try:
        return Decimal(str(invoice.get("amount"))) == Decimal(amount)
    except InvalidOperation:
        return False


class Access:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        db.executescript("""
            CREATE TABLE IF NOT EXISTS access_trials (
                user_id INTEGER, day TEXT, until INTEGER, PRIMARY KEY (user_id, day)
            );
            CREATE TABLE IF NOT EXISTS access_paid (user_id INTEGER PRIMARY KEY, until INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS access_orders (
                token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, days INTEGER NOT NULL,
                amount TEXT NOT NULL, invoice_id INTEGER UNIQUE, state TEXT NOT NULL DEFAULT 'pending',
                checked INTEGER NOT NULL DEFAULT 0, notified INTEGER NOT NULL DEFAULT 0,
                invoice_url TEXT NOT NULL DEFAULT '', expires INTEGER NOT NULL DEFAULT 0
            );
        """)
        columns = {row[1] for row in db.execute("PRAGMA table_info(access_orders)")}
        with db:
            if "notified" not in columns:
                db.execute("ALTER TABLE access_orders ADD COLUMN notified INTEGER NOT NULL DEFAULT 0")
                db.execute("UPDATE access_orders SET notified = 1 WHERE state = 'paid'")
            if "invoice_url" not in columns:
                db.execute("ALTER TABLE access_orders ADD COLUMN invoice_url TEXT NOT NULL DEFAULT ''")
            if "expires" not in columns:
                db.execute("ALTER TABLE access_orders ADD COLUMN expires INTEGER NOT NULL DEFAULT 0")

    @staticmethod
    def day(now: int) -> str:
        return datetime.fromtimestamp(now, MOSCOW).date().isoformat()

    def until(self, user_id: int, now: int | None = None) -> int:
        now = int(time()) if now is None else now
        row = self.db.execute("""
            SELECT max(until) FROM (
                SELECT until FROM access_paid WHERE user_id = ?
                UNION ALL
                SELECT until FROM access_trials WHERE user_id = ? AND (day = ? OR until > ?)
            )
        """, (user_id, user_id, self.day(now), now)).fetchone()
        return row[0] or 0

    def start_trial(self, user_id: int, now: int | None = None) -> int:
        now = int(time()) if now is None else now
        if self.until(user_id, now) > now:
            return self.until(user_id, now)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO access_trials VALUES (?, ?, ?)",
                            (user_id, self.day(now), now + TRIAL_SECONDS))
        return self.until(user_id, now)

    def active_invoice(self, user_id: int, days: int) -> str | None:
        row = self.db.execute("""
            SELECT invoice_url FROM access_orders
            WHERE user_id = ? AND days = ? AND state = 'pending' AND expires > unixepoch() AND invoice_url <> ''
            ORDER BY expires DESC LIMIT 1
        """, (user_id, days)).fetchone()
        return row[0] if row else None

    def create_order(self, user_id: int, days: int) -> str:
        token = secrets.token_urlsafe(24)
        with self.db:
            self.db.execute("INSERT INTO access_orders (token, user_id, days, amount) VALUES (?, ?, ?, ?)",
                            (token, user_id, days, PLANS[days]))
        return token

    def set_invoice(self, token: str, invoice_id: int, url: str, expires_in: int) -> None:
        with self.db:
            self.db.execute("""
                UPDATE access_orders SET invoice_id = ?, invoice_url = ?, expires = unixepoch() + ? WHERE token = ?
            """, (invoice_id, url, expires_in, token))

    def discard_order(self, token: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM access_orders WHERE token = ? AND invoice_id IS NULL", (token,))

    def pending(self) -> list[int]:
        return [row[0] for row in self.db.execute("""
            SELECT invoice_id FROM access_orders WHERE state = 'pending' AND invoice_id IS NOT NULL
            ORDER BY checked, invoice_id LIMIT 100
        """)]

    def checked(self, invoices: list[int]) -> None:
        with self.db:
            self.db.executemany("UPDATE access_orders SET checked = unixepoch() WHERE invoice_id = ?",
                                ((invoice,) for invoice in invoices))

    def payment_notices(self) -> list[tuple[int, int, int]]:
        return self.db.execute("""
            SELECT orders.invoice_id, orders.user_id, access_paid.until
            FROM access_orders AS orders JOIN access_paid USING (user_id)
            WHERE orders.state = 'paid' AND orders.notified = 0 AND access_paid.until > unixepoch()
            ORDER BY orders.invoice_id LIMIT 20
        """).fetchall()

    def mark_notified(self, invoice_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE access_orders SET notified = 1 WHERE invoice_id = ?", (invoice_id,))

    def apply_invoice(self, invoice: dict, now: int | None = None) -> bool:
        now = int(time()) if now is None else now
        invoice_id = invoice.get("invoice_id")
        if invoice.get("status") == "expired":
            with self.db:
                self.db.execute("UPDATE access_orders SET state = 'expired' WHERE invoice_id = ? AND state = 'pending'",
                                (invoice_id,))
            return False
        if invoice.get("status") != "paid":
            return False
        row = self.db.execute("""
            SELECT token, user_id, days, amount FROM access_orders
            WHERE invoice_id = ? AND state = 'pending'
        """, (invoice_id,)).fetchone()
        if row is None or not matches_order(invoice, row[0], row[3]):
            return False
        with self.db:
            changed = self.db.execute("""
                UPDATE access_orders SET state = 'paid' WHERE invoice_id = ? AND state = 'pending'
            """, (invoice_id,)).rowcount
            if not changed:
                return False
            self.db.execute("""
                INSERT INTO access_paid (user_id, until) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET until = max(access_paid.until, ?) + ?
            """, (row[1], now + row[2] * 86400, now, row[2] * 86400))
        return True
