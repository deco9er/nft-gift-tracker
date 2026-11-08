import asyncio
import json
import logging
import re
import sqlite3
from collections.abc import AsyncIterator, Callable
from dataclasses import astuple, dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from telethon import TelegramClient, errors, functions, types, utils

LOG = logging.getLogger(__name__)
SLUG = re.compile(r"[A-Za-z0-9]+-[0-9]+")
NANO = Decimal(10**9)
USERNAME = re.compile(r"[A-Za-z0-9_]{4,32}")
WALLET = re.compile(r"(?:[EUk0]Q[A-Za-z0-9_-]{46}|-?\d+:[A-Fa-f0-9]{64})")
PRICE = re.compile(r"([0-9]+(?:\.[0-9]+)?) (GRAM|Stars)\b")
TELEGRAM_PROOF_JOIN = """
    LEFT JOIN telegram_owner_proofs AS proofs ON proofs.slug = lower(lots.slug)
      AND proofs.checked >= unixepoch() - 1800
      AND proofs.profile = lower(owner_profile(lots.owner, ''))
"""
TELEGRAM_SELLER_URL = """
    CASE WHEN username_from_url(lots.owner_url) <> '' THEN lots.owner_url ELSE proofs.profile END
"""
VISIBLE_CHANGE = "source = 'tg' AND username_from_url(json_extract(lot, '$[8]')) <> ''"


@dataclass(frozen=True)
class Lot:
    slug: str
    title: str
    number: int
    price: str
    owner: str | None = None
    model: str = ""
    backdrop: str = ""
    symbol: str = ""
    owner_url: str = ""


def amount(value: Decimal) -> str:
    return format(value.normalize(), "f")


def owner_link(owner: str) -> str:
    owner = owner.strip()
    if owner.startswith("@") and USERNAME.fullmatch(owner[1:]):
        return f"https://t.me/{owner[1:]}"
    if WALLET.fullmatch(owner):
        return f"https://gram.org/address/{owner}"
    return ""


def owner_profile(owner: str | None, url: str | None) -> str:
    return url or owner_link(owner or "")


def username_from_url(profile: str | None) -> str:
    if profile and profile.startswith("https://t.me/"):
        username = profile.removeprefix("https://t.me/")
        if USERNAME.fullmatch(username):
            return username
    return ""


def public_username(lot: Lot) -> str:
    return username_from_url(owner_profile(lot.owner, lot.owner_url))


def price_value(price: str, currency: str) -> float | None:
    return next((float(value) for value, unit in PRICE.findall(price) if unit == currency), None)


def owner_type(owner: str | None, url: str | None) -> str:
    url = url or owner_link(owner or "")
    if url.startswith("https://t.me/"):
        return "user"
    if url.startswith("tg://user?id="):
        return "id"
    if url.startswith("https://gram.org/address/"):
        return "wallet"
    return "name" if owner else "hidden"


def telegram_lot(gift: types.StarGiftUnique, peers: dict) -> Lot:
    stars = gram = ""
    for price in gift.resell_amount or []:
        if isinstance(price, types.StarsAmount) and not gift.resale_ton_only:
            stars = f"{amount(Decimal(price.amount) + Decimal(price.nanos) / NANO)} Stars"
        if isinstance(price, types.StarsTonAmount):
            gram = f"{amount(Decimal(price.amount) / NANO)} GRAM"
    owner = gift.owner_address or gift.owner_name or ""
    owner_url = owner_link(gift.owner_address or "")
    if not gift.owner_address and isinstance(gift.owner_id, types.PeerUser):
        peer = peers.get(utils.get_peer_id(gift.owner_id))
        username = getattr(peer, "username", None)
        if peer:
            username = username or next((item.username for item in (getattr(peer, "usernames", None) or [])
                                         if item.active), None)
            owner = f"@{username}" if username else (utils.get_display_name(peer) or owner)
        if username:
            owner_url = owner_link(f"@{username}")
        else:
            owner_url = f"tg://user?id={gift.owner_id.user_id}"
    fields = {
        types.StarGiftAttributeModel: "model",
        types.StarGiftAttributeBackdrop: "backdrop",
        types.StarGiftAttributePattern: "symbol",
    }
    traits = {}
    for attribute in gift.attributes:
        if field := fields.get(type(attribute)):
            rarity = getattr(attribute.rarity, "permille", None)
            suffix = f" {amount(Decimal(rarity) / 10)}%" if rarity is not None else ""
            traits[field] = attribute.name + suffix
    prices = " · ".join(filter(None, (gram, stars)))
    return Lot(gift.slug, gift.title, gift.num, prices, owner or None, owner_url=owner_url, **traits)


async def verify_telegram_seller(client: TelegramClient, profile: str, candidates: set[str]) -> set[str]:
    username = username_from_url(profile)
    if not username:
        return set()
    resolved = await asyncio.wait_for(client(functions.contacts.ResolveUsernameRequest(username)), 6)
    if not isinstance(resolved.peer, types.PeerUser):
        return set()
    user_id = resolved.peer.user_id
    user = next((user for user in resolved.users if user.id == user_id), None)
    if user is None:
        return set()
    peer = utils.get_input_peer(user)
    offset, offsets, confirmed = "", set(), set()
    for _ in range(8):
        request = functions.payments.GetSavedStarGiftsRequest(peer, offset, 100, exclude_hosted=True)
        try:
            page = await asyncio.wait_for(client(request), 6)
        except (OSError, TimeoutError):
            if confirmed:
                return confirmed
            raise
        for saved in page.gifts:
            gift = saved.gift
            if not isinstance(gift, types.StarGiftUnique) or gift.owner_address or gift.host_id:
                continue
            if gift.owner_id != types.PeerUser(user_id):
                continue
            slug = gift.slug.lower()
            if slug in candidates:
                confirmed.add(slug)
        offset = page.next_offset
        if not offset or offset in offsets or confirmed == candidates:
            break
        offsets.add(offset)
        await asyncio.sleep(0.6)
    return confirmed


async def telegram_batches(client: TelegramClient, incomplete: list[str] | None = None,
                           progress: Callable[[str], None] | None = None,
                           completed: set[str] | None = None,
                           checkpoint: Callable[[str], None] | None = None,
                           resale_request=None) -> AsyncIterator[list[Lot]]:
    resale_request = resale_request or client
    while True:
        try:
            catalog = await client(functions.payments.GetStarGiftsRequest(hash=0))
            break
        except errors.FloodWaitError as exc:
            LOG.warning("Telegram: каталог подарков, пауза %d с (FloodWait)", exc.seconds)
            if progress:
                progress(f"Каталог подарков · пауза Telegram {exc.seconds} с")
            await asyncio.sleep(exc.seconds + 1)
    if not isinstance(catalog, types.payments.StarGifts) or not catalog.gifts:
        raise ValueError("Telegram вернул пустой каталог")
    if incomplete is not None:
        incomplete.clear()
    collections = [gift for gift in catalog.gifts
                   if isinstance(gift, types.StarGift) and gift.availability_resale]
    for index, collection in enumerate(collections, 1):
        section = str(collection.id)
        if completed and section in completed:
            continue
        title = collection.title or str(collection.id)
        if progress:
            progress(f"Коллекция {index}/{len(collections)} · {title}")
        slugs = set()
        for sort_by_num in (True, None):
            offset, seen = "", set()
            while True:
                try:
                    page = await resale_request(functions.payments.GetResaleStarGiftsRequest(
                        gift_id=collection.id, offset=offset, limit=100, sort_by_num=sort_by_num,
                    ))
                except errors.FloodWaitError as exc:
                    LOG.info("Telegram запросил паузу на %d с", exc.seconds)
                    if progress:
                        progress(f"Коллекция {index}/{len(collections)} · пауза Telegram {exc.seconds} с")
                    await asyncio.sleep(exc.seconds + 1)
                    if progress:
                        progress(f"Коллекция {index}/{len(collections)} · {title}")
                    continue
                peers = {utils.get_peer_id(p): p for p in [*page.users, *page.chats]}
                lots = [
                    telegram_lot(gift, peers) for gift in page.gifts
                    if isinstance(gift, types.StarGiftUnique) and SLUG.fullmatch(gift.slug)
                ]
                slugs.update(lot.slug.lower() for lot in lots)
                yield lots
                offset = page.next_offset
                await asyncio.sleep(0.45)
                if not offset:
                    break
                if offset in seen:
                    if len(slugs) >= page.count:
                        offset = ""
                    else:
                        LOG.warning("Telegram: повтор курсора в %s (%d/%d лотов, sort_by_num=%s)",
                                    title, len(slugs), page.count, sort_by_num)
                    break
                seen.add(offset)
            if not offset:
                if checkpoint:
                    checkpoint(section)
                break
        else:
            if incomplete is not None:
                incomplete.append(title)


class Catalog:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        db.create_function("price_value", 2, price_value, deterministic=True)
        db.create_function("owner_type", 2, owner_type, deterministic=True)
        db.create_function("owner_profile", 2, owner_profile, deterministic=True)
        db.create_function("username_from_url", 1, username_from_url, deterministic=True)
        db.executescript("""
            CREATE TABLE IF NOT EXISTS lots (
                source TEXT, slug TEXT, title TEXT, number INTEGER, price TEXT, owner TEXT,
                model TEXT, backdrop TEXT, symbol TEXT, owner_url TEXT DEFAULT '',
                seen_run TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (source, slug)
            );
            CREATE INDEX IF NOT EXISTS lots_order ON lots (source, title, number, slug);
            CREATE TABLE IF NOT EXISTS snapshots (
                source TEXT PRIMARY KEY, updated TEXT, baseline INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS changes (
                id INTEGER PRIMARY KEY, source TEXT, observed TEXT, lot TEXT, previous_price TEXT,
                sent INTEGER NOT NULL DEFAULT 0, feed_sent INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS pending_changes ON changes (id) WHERE sent = 0;
            CREATE TABLE IF NOT EXISTS seller_ratings (
                profile TEXT PRIMARY KEY, checked INTEGER NOT NULL, level INTEGER
            );
            CREATE TABLE IF NOT EXISTS telegram_owner_proofs (
                slug TEXT PRIMARY KEY, profile TEXT NOT NULL, checked INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS telegram_proof_profile ON telegram_owner_proofs (profile);
            CREATE TABLE IF NOT EXISTS telegram_seller_checks (
                profile TEXT PRIMARY KEY, checked INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scan_runs (
                source TEXT PRIMARY KEY, token TEXT NOT NULL, started INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scan_sections (
                source TEXT NOT NULL, section TEXT NOT NULL, PRIMARY KEY (source, section)
            );
            CREATE TABLE IF NOT EXISTS live_market_heads (
                gift_id INTEGER PRIMARY KEY, slugs TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS live_market_lots (
                slug TEXT PRIMARY KEY, checked INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS lots_identity ON lots (source, lower(slug));
        """)
        lot_columns = {row[1] for row in db.execute("PRAGMA table_info(lots)")}
        with db:
            if "owner_url" not in lot_columns:
                db.execute("ALTER TABLE lots ADD COLUMN owner_url TEXT DEFAULT ''")
            if "seen_run" not in lot_columns:
                db.execute("ALTER TABLE lots ADD COLUMN seen_run TEXT NOT NULL DEFAULT ''")
            db.execute("CREATE INDEX IF NOT EXISTS lots_owner ON lots (source, owner_url)")
            if "baseline" not in {row[1] for row in db.execute("PRAGMA table_info(snapshots)")}:
                db.execute("ALTER TABLE snapshots ADD COLUMN baseline INTEGER NOT NULL DEFAULT 1")
            if "feed_sent" not in {row[1] for row in db.execute("PRAGMA table_info(changes)")}:
                db.execute("ALTER TABLE changes ADD COLUMN feed_sent INTEGER NOT NULL DEFAULT 0")
                db.execute("UPDATE changes SET feed_sent = sent WHERE sent = 1")
            db.execute("CREATE INDEX IF NOT EXISTS pending_feed ON changes (id) WHERE feed_sent = 0")
            db.execute("""
                CREATE INDEX IF NOT EXISTS telegram_change_slug ON changes (lower(json_extract(lot, '$[0]')))
                WHERE source = 'tg'
            """)
            for table in ("lots", "changes", "snapshots", "scan_runs", "scan_sections"):
                db.execute(f"DELETE FROM {table} WHERE source = 'fr'")
            db.execute("DROP INDEX IF EXISTS fragment_change_slug")
            db.execute("DROP TABLE IF EXISTS fragment_profiles")

    def active_scan_token(self) -> str:
        row = self.db.execute("SELECT token FROM scan_runs WHERE source = 'tg'").fetchone()
        return row[0] if row else ""

    def live_page(self, gift_id: int, lots: list[Lot]) -> None:
        row = self.db.execute("SELECT slugs FROM live_market_heads WHERE gift_id = ?", (gift_id,)).fetchone()
        old = set(json.loads(row[0])) if row else set()
        slugs = [lot.slug.lower() for lot in lots]
        first_known = next((index for index, slug in enumerate(slugs) if slug in old), 0)
        token = self.active_scan_token()
        self.observe("tg", lots[:first_known], True, token)
        self.observe("tg", lots[first_known:], False, token)
        with self.db:
            self.db.executemany("REPLACE INTO live_market_lots VALUES (?, unixepoch())", ((slug,) for slug in slugs))
            self.db.execute("REPLACE INTO live_market_heads VALUES (?, ?)", (gift_id, json.dumps(slugs)))
            self.db.execute("DELETE FROM live_market_lots WHERE checked < unixepoch() - 900")

    def discard_live_listing(self, slug: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM lots WHERE source = 'tg' AND lower(slug) = ?", (slug.lower(),))
            self.db.execute("DELETE FROM live_market_lots WHERE slug = ?", (slug.lower(),))

    def has_baseline(self, source: str) -> bool:
        row = self.db.execute("SELECT baseline FROM snapshots WHERE source = ?", (source,)).fetchone()
        return bool(row and row[0])

    def start_scan(self, source: str) -> tuple[str, set[str], int]:
        row = self.db.execute("""
            SELECT token FROM scan_runs WHERE source = ? AND started >= unixepoch() - 86400
        """, (source,)).fetchone()
        if row is None:
            token = uuid4().hex
            with self.db:
                self.db.execute("DELETE FROM scan_sections WHERE source = ?", (source,))
                self.db.execute("REPLACE INTO scan_runs VALUES (?, ?, unixepoch())", (source, token))
            return token, set(), 0
        token = row[0]
        sections = {row[0] for row in self.db.execute(
            "SELECT section FROM scan_sections WHERE source = ?", (source,)
        )}
        count = self.db.execute("SELECT count(*) FROM lots WHERE source = ? AND seen_run = ?",
                                (source, token)).fetchone()[0]
        return token, sections, count

    def checkpoint(self, source: str, section: str) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO scan_sections VALUES (?, ?)", (source, section))

    def observe(self, source: str, lots: list[Lot], baseline: bool, token: str) -> int:
        batch = {lot.slug.lower(): lot for lot in lots}
        if not batch:
            return 0
        placeholders = ", ".join("?" for _ in batch)
        rows = self.db.execute(
            f"SELECT lower(slug), slug, price, seen_run, owner_url FROM lots "
            f"WHERE source = ? AND lower(slug) IN ({placeholders})",
            (source, *batch),
        ).fetchall()
        previous = {key: price for key, _, price, _, _ in rows}
        seen = {key for key, _, _, seen_run, _ in rows if seen_run == token}
        proofs = dict(self.db.execute(f"""
            SELECT slug, profile FROM telegram_owner_proofs
            WHERE slug IN ({placeholders}) AND checked >= unixepoch() - 1800
        """, tuple(batch))) if source == "tg" else {}
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        events = []
        for key, lot in batch.items():
            old_price = previous.get(key)
            if not lot.price or not ((baseline and key not in previous) or (old_price and old_price != lot.price)):
                continue
            if source == "tg" and not username_from_url(lot.owner_url):
                candidate = owner_link(lot.owner or "").lower()
                if not username_from_url(candidate):
                    continue
                if proofs.get(key) == candidate:
                    lot = replace(lot, owner_url=candidate)
            events.append((source, now, json.dumps(astuple(lot), ensure_ascii=False), old_price))
        with self.db:
            self.db.executemany(
                "INSERT INTO changes (source, observed, lot, previous_price) VALUES (?, ?, ?, ?)",
                events,
            )
            self.db.executemany("DELETE FROM lots WHERE source = ? AND slug = ?", (
                (source, slug) for key, slug, _, _, _ in rows if slug != batch[key].slug
            ))
            self.db.executemany("""
                REPLACE INTO lots (source, slug, title, number, price, owner, model, backdrop,
                                   symbol, owner_url, seen_run) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                (source, *astuple(lot), token) for lot in batch.values()
            ))
        return len(batch) - len(seen)

    def finish_scan(self, source: str, token: str, complete: bool) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.db:
            if complete:
                self.db.execute("DELETE FROM lots WHERE source = ? AND seen_run <> ?", (source, token))
            self.db.execute("""
                INSERT INTO snapshots (source, updated, baseline) VALUES (?, ?, ?)
                ON CONFLICT(source) DO UPDATE SET updated=excluded.updated,
                    baseline=max(snapshots.baseline, excluded.baseline)
            """, (source, now, complete))
            self.db.execute("DELETE FROM scan_sections WHERE source = ?", (source,))
            self.db.execute("DELETE FROM scan_runs WHERE source = ? AND token = ?", (source, token))

    def page(self, source: str, page: int, size: int, collection: str = "",
             currency: str = "", minimum: float = 0, maximum: float | None = None,
             owner_filter: str = "", rating_filter: str = "", sort: str = "",
             floor_range: tuple[int, int] | None = None):
        where = ["source = ?"]
        params = [source]
        if collection:
            where.append("title = ?")
            params.append(collection)
        if currency:
            where.append("price_value(price, ?) >= ?")
            params.extend((currency, minimum))
            if maximum is not None:
                where.append("price_value(price, ?) < ?")
                params.extend((currency, maximum))
        join = TELEGRAM_PROOF_JOIN
        url = TELEGRAM_SELLER_URL
        fields = f"owner, {url}, model, backdrop, symbol"
        where.append(f"({url}) IS NOT NULL")
        level = "NULL"
        if rating_filter or sort.startswith("rating"):
            join += f""" LEFT JOIN seller_ratings AS ratings ON ratings.profile = ({url})
                        AND ratings.checked >= unixepoch() - 86400"""
            level = "ratings.level"
        if sort == "live":
            join += " JOIN live_market_lots AS live ON live.slug = lower(lots.slug)"
            where.append("live.checked >= unixepoch() - 900")
        if owner_filter:
            category = f"owner_type({owner}, {url})"
            allowed = {
                "no_wallet": ("user", "id", "name"),
                "no_username": ("id", "name"),
            }.get(owner_filter, (owner_filter,))
            placeholders = ", ".join("?" for _ in allowed)
            where.append(f"{category} IN ({placeholders})")
            params.extend(allowed)
        if rating_filter in ("rated", "unrated"):
            operator = "IS NOT NULL" if rating_filter == "rated" else "IS NULL"
            where.append(f"{level} {operator}")
        elif rating_filter.isascii() and rating_filter.isdecimal():
            where.append(f"{level} >= ?")
            params.append(int(rating_filter))
        if floor_range or sort.startswith("floor"):
            join += """
                LEFT JOIN (
                    SELECT source AS floor_source, title AS floor_title, min(price_value(price, 'GRAM')) AS floor
                    FROM lots WHERE source = ? AND price NOT LIKE '% · аукцион' GROUP BY title
                ) AS floors ON floors.floor_source = lots.source AND floors.floor_title = lots.title
            """
            params.insert(0, source)
        if floor_range:
            where.append("floor > ? AND floor <= ?")
            params.extend(floor_range)
        clause = " AND ".join(where)
        row = self.db.execute("SELECT updated FROM snapshots WHERE source = ?", (source,)).fetchone()
        total = self.db.execute(f"SELECT count(*) FROM lots {join} WHERE {clause}", params).fetchone()[0]
        page = min(max(0, page), max(0, (total - 1) // size))
        order = {
            "live": "live.checked DESC, title, number, lots.slug",
            "title_desc": "title DESC, number, lots.slug",
            "number_asc": "number, title, lots.slug",
            "number_desc": "number DESC, title, lots.slug",
            "rating_asc": f"{level} ASC NULLS LAST, title, number, lots.slug",
            "rating_desc": f"{level} DESC NULLS LAST, title, number, lots.slug",
            "floor_asc": "floor ASC NULLS LAST, title, number, lots.slug",
            "floor_desc": "floor DESC NULLS LAST, title, number, lots.slug",
        }.get(sort, "title, number, lots.slug")
        if sort in {"stars_asc", "stars_desc", "gram_asc", "gram_desc"}:
            direction = "DESC" if sort.endswith("_desc") else "ASC"
            order = f"price_value(price, ?) {direction} NULLS LAST, title, number, lots.slug"
            params.append("Stars" if sort.startswith("stars") else "GRAM")
        rows = self.db.execute(f"""
            SELECT lots.slug, title, number, price, {fields} FROM lots {join} WHERE {clause}
            ORDER BY {order} LIMIT ? OFFSET ?
        """, (*params, size, page * size))
        lots = [Lot(slug, title, number, price, owner, model, backdrop, symbol, url)
                for slug, title, number, price, owner, url, model, backdrop, symbol in rows]
        return lots, page, total, row[0] if row else None

    def collections(self, source: str) -> list[str]:
        join = TELEGRAM_PROOF_JOIN
        visible = f"({TELEGRAM_SELLER_URL}) IS NOT NULL"
        return [row[0] for row in self.db.execute(f"""
            SELECT DISTINCT title FROM lots {join} WHERE source = ? AND ({visible}) ORDER BY title
        """, (source,))]

    def next_telegram_seller(self) -> tuple[str, set[str]] | None:
        row = self.db.execute("""
            WITH candidates AS (
                SELECT lower(owner_profile(owner, '')) AS profile,
                       (live.checked >= unixepoch() - 300 AND proofs.slug IS NULL) AS urgent
                FROM lots LEFT JOIN live_market_lots AS live ON live.slug = lower(lots.slug)
                LEFT JOIN telegram_owner_proofs AS proofs ON proofs.slug = lower(lots.slug)
                    AND proofs.checked >= unixepoch() - 1800
                WHERE source = 'tg' AND ltrim(owner) LIKE '@%' AND username_from_url(owner_url) = ''
            )
            SELECT candidates.profile FROM candidates
            LEFT JOIN telegram_seller_checks AS checks ON checks.profile = candidates.profile
            WHERE username_from_url(candidates.profile) <> ''
            GROUP BY candidates.profile
            HAVING coalesce(checks.checked, 0) < unixepoch() - CASE WHEN max(urgent) THEN 120 ELSE 900 END
            ORDER BY coalesce(max(urgent), 0) DESC, coalesce(checks.checked, 0), count(*) DESC LIMIT 1
        """).fetchone()
        if row is None:
            return None
        profile = row[0]
        slugs = {row[0] for row in self.db.execute("""
            SELECT lower(slug) FROM lots WHERE source = 'tg' AND lower(owner_profile(owner, '')) = ?
        """, (profile,))}
        return profile, slugs

    def save_telegram_seller_proofs(self, profile: str, slugs: set[str]) -> None:
        with self.db:
            self.db.execute("REPLACE INTO telegram_seller_checks VALUES (?, unixepoch())", (profile,))
            self.db.execute("DELETE FROM telegram_owner_proofs WHERE profile = ?", (profile,))
            self.db.executemany("REPLACE INTO telegram_owner_proofs VALUES (?, ?, unixepoch())",
                                ((slug, profile) for slug in slugs))
            self.db.executemany("""
                UPDATE changes SET lot = json_set(lot, '$[8]', ?)
                WHERE source = 'tg' AND lower(json_extract(lot, '$[0]')) = ?
                  AND lower(owner_profile(json_extract(lot, '$[4]'), '')) = ?
                  AND username_from_url(json_extract(lot, '$[8]')) = ''
                  AND unixepoch(observed) >= unixepoch() - 1800
            """, ((profile, slug, profile) for slug in slugs))

    def current_telegram_seller(self, slug: str) -> str:
        row = self.db.execute(f"""
            SELECT {TELEGRAM_SELLER_URL} FROM lots {TELEGRAM_PROOF_JOIN}
            WHERE source = 'tg' AND lower(lots.slug) = ?
        """, (slug.lower(),)).fetchone()
        return row[0] if row and row[0] else ""

    def floors(self, source: str, titles: list[str], currency: str = "GRAM") -> dict[str, float]:
        if not titles:
            return {}
        placeholders = ", ".join("?" for _ in titles)
        return {title: floor for title, floor in self.db.execute(f"""
            SELECT title, min(price_value(price, ?)) FROM lots
            WHERE source = ? AND title IN ({placeholders}) AND price NOT LIKE '% · аукцион'
            GROUP BY title
        """, (currency, source, *titles)) if floor is not None}

    def seller_rating(self, profile: str) -> tuple[int | None] | None:
        return self.db.execute("""
            SELECT level FROM seller_ratings WHERE profile = ?
              AND checked >= unixepoch() - CASE WHEN level IS NULL THEN 900 ELSE 86400 END
        """, (profile,)).fetchone()

    def save_seller_rating(self, profile: str, level: int | None) -> None:
        with self.db:
            self.db.execute("REPLACE INTO seller_ratings VALUES (?, unixepoch(), ?)", (profile, level))

    def seller_rating_queue(self, limit: int = 12) -> list[str]:
        return [row[0] for row in self.db.execute(f"""
            WITH profiles AS (
                SELECT DISTINCT {TELEGRAM_SELLER_URL} AS profile FROM lots {TELEGRAM_PROOF_JOIN}
                WHERE source = 'tg'
            )
            SELECT profiles.profile FROM profiles
            LEFT JOIN seller_ratings AS ratings ON ratings.profile = profiles.profile
            WHERE profiles.profile IS NOT NULL
              AND coalesce(ratings.checked, 0) < unixepoch() - CASE WHEN ratings.level IS NULL THEN 900 ELSE 86400 END
            ORDER BY profiles.profile LIMIT ?
        """, (limit,))]

    def changes(self, page: int, size: int):
        total = self.db.execute(f"""
            SELECT count(*) FROM changes WHERE {VISIBLE_CHANGE}
        """).fetchone()[0]
        page = min(max(0, page), max(0, (total - 1) // size))
        rows = self.db.execute(f"""
            SELECT source, observed, lot, previous_price FROM changes
            WHERE {VISIBLE_CHANGE}
            ORDER BY id DESC LIMIT ? OFFSET ?
        """, (size, page * size))
        return [(source, observed, Lot(*json.loads(lot)), previous) for source, observed, lot, previous in rows], page, total

    def pending(self, limit: int = 20, *, feed: bool = False):
        column = "feed_sent" if feed else "sent"
        rows = self.db.execute(f"""
            SELECT id, source, observed, lot, previous_price FROM changes
            WHERE {column} = 0 AND ({VISIBLE_CHANGE})
            AND unixepoch(observed) >= unixepoch() - 1800
            ORDER BY id DESC LIMIT ?
        """, (limit,))
        return [(id_, source, observed, Lot(*json.loads(lot)), prev)
                for id_, source, observed, lot, prev in rows]

    def mark_sent(self, last: int, *, feed: bool = False) -> None:
        column = "feed_sent" if feed else "sent"
        with self.db:
            self.db.execute(f"UPDATE changes SET {column} = 1 WHERE id = ?", (last,))

    def skip_group_change(self, change_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE changes SET sent = 1, feed_sent = 1 WHERE id = ?", (change_id,))

    def lot_counts(self) -> dict[str, int]:
        return dict(self.db.execute("SELECT source, count(*) FROM lots GROUP BY source"))
