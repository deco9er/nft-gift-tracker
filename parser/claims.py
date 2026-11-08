import json

from market import Lot


class Claims:
    def __init__(self, db):
        self.db = db
        db.executescript("""
            CREATE TABLE IF NOT EXISTS tracker_claims (
                slug TEXT, seller TEXT, user_id INTEGER NOT NULL, username TEXT NOT NULL,
                body TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0, next_try INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (slug, seller)
            );
            CREATE TABLE IF NOT EXISTS tracker_posts (
                chat_id INTEGER, message_id INTEGER, event_id INTEGER NOT NULL,
                slug TEXT NOT NULL, seller TEXT NOT NULL, body TEXT NOT NULL, shown INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS tracker_posts_lot ON tracker_posts (slug, seller);
        """)

    def occupant(self, lot: Lot) -> str:
        row = self.db.execute("SELECT username FROM tracker_claims WHERE slug = ? AND seller = ?",
                              (lot.slug.lower(), lot.owner_url.lower())).fetchone()
        return row[0] if row else ""

    def reserve(self, lot: Lot, user_id: int, username: str, body: str) -> bool:
        with self.db:
            return bool(self.db.execute("""
                INSERT OR IGNORE INTO tracker_claims (slug, seller, user_id, username, body) VALUES (?, ?, ?, ?, ?)
            """, (lot.slug.lower(), lot.owner_url.lower(), user_id, username, body)).rowcount)

    def record_post(self, chat_id: int, message_id: int, event_id: int, lot: Lot, body: str, shown: bool) -> None:
        with self.db:
            self.db.execute("INSERT INTO tracker_posts VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (chat_id, message_id, event_id, lot.slug.lower(), lot.owner_url.lower(), body, shown))

    def posted_lot(self, chat_id: int, message_id: int, event_id: int) -> Lot | None:
        row = self.db.execute("""
            SELECT changes.lot FROM tracker_posts AS posts JOIN changes ON changes.id = posts.event_id
            WHERE posts.chat_id = ? AND posts.message_id = ? AND posts.event_id = ?
        """, (chat_id, message_id, event_id)).fetchone()
        return Lot(*json.loads(row[0])) if row else None

    def posts_to_edit(self) -> list[tuple]:
        return self.db.execute("""
            SELECT posts.chat_id, posts.message_id, posts.body, claims.username
            FROM tracker_posts AS posts JOIN tracker_claims AS claims USING (slug, seller)
            WHERE posts.shown = 0 LIMIT 20
        """).fetchall()

    def mark_post_edited(self, chat_id: int, message_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE tracker_posts SET shown = 1 WHERE chat_id = ? AND message_id = ?",
                            (chat_id, message_id))

    def notices(self) -> list[tuple]:
        return self.db.execute("""
            SELECT slug, seller, user_id, body FROM tracker_claims
            WHERE sent = 0 AND next_try <= unixepoch() ORDER BY next_try LIMIT 20
        """).fetchall()

    def mark_notice(self, slug: str, seller: str, sent: bool) -> None:
        with self.db:
            self.db.execute("UPDATE tracker_claims SET sent = ?, next_try = unixepoch() + 60 "
                            "WHERE slug = ? AND seller = ?", (sent, slug, seller))
