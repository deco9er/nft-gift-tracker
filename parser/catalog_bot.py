import argparse
import asyncio
import logging
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from html import escape
from logging.handlers import RotatingFileHandler
from pathlib import Path
from time import time

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiohttp import ClientError, ClientSession, ClientTimeout
from dotenv import load_dotenv
from telethon import TelegramClient, errors, functions, types, utils

from access import PLANS, Access, access_db_path
from checkout import INVOICE_TTL, CryptoPay, CryptoPayError, check_payments, poll_payments
from claims import Claims
from filters import FLOORS, Filters, PRICE_BANDS, RATINGS, SORTS, collection_key, filter_keyboard, floor_category
from forum import FEED_KEY, FEED_NAME, TrackerForum
from group_access import GroupAccess
from instance import instance_lock
from market import (
    Catalog, Lot, SLUG, USERNAME, owner_profile, public_username,
    telegram_batches, telegram_lot, username_from_url, verify_telegram_seller,
)
from proxies import telegram_proxy

ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger(__name__)
LABELS = {"tg": "Telegram Market"}
PAGE_SIZE = 5
MOSCOW = timezone(timedelta(hours=3))
EMOJI = {
    "gift": ("6032644646587338669", "🎁"),
    "price": ("5778421276024509124", "💰"),
    "model": ("6021792097454002931", "🪄"),
    "backdrop": ("6030466823290360017", "🖼"),
    "symbol": ("5778226250149532337", "✨"),
    "owner": ("5870994129244131212", "👤"),
    "change": ("5870891312022032055", "📈"),
}
CUSTOM_EMOJI = re.compile(r'<tg-emoji emoji-id="[0-9]+">([^<>]*)</tg-emoji>')
USER_LINK = re.compile(r'<a href="tg://user\?id=[0-9]+">([^<>]*)</a>')
PRICE_TOPICS = {f"price:{key}": label for key, (label, _, _) in FLOORS.items()}


def read_int(name: str, default: str = "", minimum: int = 1) -> int:
    value = os.getenv(name) or default
    if not value.isdecimal() or int(value) < minimum:
        raise SystemExit(f"В .env укажите {name} — целое число от {minimum}.")
    return int(value)


def read_admin_ids() -> list[int]:
    values = [value.strip() for value in os.getenv("ADMIN_ID", "").split(",")]
    if any(not value.isdecimal() or int(value) < 1 for value in values):
        raise SystemExit("В .env укажите числовой ADMIN_ID или несколько ID через запятую.")
    return [int(value) for value in values]


def telegram_client() -> TelegramClient:
    api_hash = os.getenv("API_HASH", "")
    if not api_hash:
        raise SystemExit("В .env укажите API_HASH с my.telegram.org.")
    return TelegramClient(str(ROOT / "market"), read_int("API_ID"), api_hash, receive_updates=False,
                          proxy=telegram_proxy(), flood_sleep_threshold=0)


class GiftList:
    def __init__(self, catalog: Catalog, forum: TrackerForum):
        self.catalog = catalog
        self.forum = forum
        self.claims = Claims(forum.db)
        self.status = dict.fromkeys(LABELS, "Собираю лоты…")
        self.rating_slots = asyncio.Semaphore(1)
        self.rating_paused_until = 0.0
        self.rating_priority: dict[str, None] = {}
        self.icons = {role: fallback for role, (_, fallback) in EMOJI.items()}
        self.client: TelegramClient | None = None
        self.admins: set[int] = set()
        self.resale_lock = asyncio.Lock()
        self.resale_after = 0.0
        self.listing_after = 0.0
        self.filters: dict[tuple[int, str], Filters] = {}

    async def setup_emoji(self, bot: Bot) -> None:
        ids = {role: os.getenv(f"{role.upper()}_EMOJI_ID", emoji_id).strip()
               for role, (emoji_id, _) in EMOJI.items()}
        ids = {role: value for role, value in ids.items() if re.fullmatch(r"[0-9]{1,20}", value)}
        if not ids:
            return
        try:
            stickers = await bot.get_custom_emoji_stickers(custom_emoji_ids=list(set(ids.values())))
        except TelegramAPIError:
            return
        alternatives = {sticker.custom_emoji_id: sticker.emoji for sticker in stickers}
        for role, emoji_id in ids.items():
            if alt := alternatives.get(emoji_id):
                self.icons[role] = f'<tg-emoji emoji-id="{emoji_id}">{escape(alt)}</tg-emoji>'

    async def scan(self, source, batches, interval: int, incomplete: list[str]) -> None:
        while True:
            lot_count = 0
            pages = 0
            stage = "Подключение"
            next_log = 0.0
            loop = asyncio.get_running_loop()

            def report(detail: str = "") -> None:
                nonlocal stage, next_log
                if detail:
                    stage = detail
                self.status[source] = f"Обход · {stage}\nСтраниц за запуск · {pages} | Лотов в обходе · {lot_count}"
                if loop.time() >= next_log:
                    LOG.info("%s: %s | страниц %d, лотов %d", LABELS[source], stage, pages, lot_count)
                    next_log = loop.time() + 15

            try:
                token, completed, lot_count = self.catalog.start_scan(source)
                if completed:
                    report(f"Продолжение · готовых разделов {len(completed)}")
                else:
                    report()
                baseline = self.catalog.has_baseline(source)
                async for batch in batches(report, completed, lambda key: self.catalog.checkpoint(source, key)):
                    pages += 1
                    lot_count += self.catalog.observe(source, batch, baseline, token)
                    report()
                self.catalog.finish_scan(source, token, complete=not incomplete)
            except Exception:
                LOG.exception("Обход %s завершился ошибкой", LABELS[source])
                self.status[source] = "Ошибка обновления."
                await asyncio.sleep(min(interval, 300))
                continue
            self.status[source] = (f"Неполный обход · проблемных разделов: {len(incomplete)}."
                                   if incomplete else "")
            LOG.info("%s: обход завершён | страниц %d, лотов %d, неполных разделов %d",
                     LABELS[source], pages, lot_count, len(incomplete))
            await asyncio.sleep(interval)

    async def scan_telegram(self, interval: int) -> None:
        if not os.getenv("API_ID") or not os.getenv("API_HASH"):
            self.status["tg"] = "Укажите API_ID и API_HASH в .env."
            return
        client = telegram_client()
        incomplete = []
        try:
            while True:
                try:
                    await client.connect()
                    if not await client.is_user_authorized() or (await client.get_me()).bot:
                        self.status["tg"] = "Войдите в пользовательский аккаунт через --login."
                        return
                    self.client = client
                    await self.scan("tg", lambda report, completed, checkpoint: telegram_batches(
                                        client, incomplete, report, completed, checkpoint, self.resale_request),
                                    interval, incomplete)
                except (OSError, TimeoutError):
                    self.status["tg"] = "Ошибка соединения с Telegram."
                    LOG.warning("Не удалось подключиться к Telegram")
                    await asyncio.sleep(60)
        finally:
            self.client = None
            await client.disconnect()

    async def resale_request(self, request):
        async with self.resale_lock:
            loop = asyncio.get_running_loop()
            await asyncio.sleep(max(0, self.resale_after - loop.time()))
            try:
                return await self.client(request)
            except errors.FloodWaitError as exc:
                self.resale_after = loop.time() + exc.seconds + 1
                raise
            finally:
                self.resale_after = max(self.resale_after, loop.time() + 0.45)

    async def watch_live(self, interval: int) -> None:
        while True:
            client = self.client
            if client is None:
                await asyncio.sleep(5)
                continue
            try:
                catalog = await client(functions.payments.GetStarGiftsRequest(hash=0))
                collections = [gift.id for gift in catalog.gifts
                               if isinstance(gift, types.StarGift) and gift.availability_resale]
                for gift_id in collections:
                    page = await self.resale_request(functions.payments.GetResaleStarGiftsRequest(gift_id, "", 25))
                    peers = {utils.get_peer_id(peer): peer for peer in [*page.users, *page.chats]}
                    lots = [telegram_lot(gift, peers) for gift in page.gifts
                            if isinstance(gift, types.StarGiftUnique) and SLUG.fullmatch(gift.slug)]
                    self.catalog.live_page(gift_id, lots)
                    await asyncio.sleep(interval)
                LOG.info("Лайв: проверено коллекций %d", len(collections))
                await asyncio.sleep(5)
            except errors.FloodWaitError as exc:
                LOG.warning("Лайв: Telegram попросил паузу на %d с", exc.seconds)
                await asyncio.sleep(exc.seconds + 1)
            except (OSError, TimeoutError, errors.RPCError) as exc:
                LOG.warning("Лайв: обход будет повторён (%s)", type(exc).__name__)
                await asyncio.sleep(15)

    async def refresh_listing(self, lot: Lot) -> Lot | None:
        client = self.client
        loop = asyncio.get_running_loop()
        if client is None or loop.time() < self.listing_after:
            raise TimeoutError
        try:
            result = await asyncio.wait_for(client(functions.payments.GetUniqueStarGiftRequest(lot.slug)), 5)
        except errors.FloodWaitError as exc:
            self.listing_after = loop.time() + exc.seconds + 1
            LOG.warning("Лайв: проверка лотов приостановлена на %d с (FloodWait)", exc.seconds)
            raise
        except errors.RPCError as exc:
            if exc.message not in ("STARGIFT_SLUG_INVALID", "STARGIFT_INVALID"):
                raise
            self.catalog.discard_live_listing(lot.slug)
            return None
        gift = result.gift
        if not isinstance(gift, types.StarGiftUnique):
            return None
        peers = {utils.get_peer_id(peer): peer for peer in [*result.users, *result.chats]}
        live = telegram_lot(gift, peers)
        if not live.price:
            self.catalog.discard_live_listing(lot.slug)
            return None
        self.catalog.observe("tg", [live], False, self.catalog.active_scan_token())
        seller = self.catalog.current_telegram_seller(lot.slug)
        if not seller or seller.lower() != lot.owner_url.lower():
            return None
        return replace(live, owner_url=seller)

    async def lookup_rating(self, profile: str) -> int | None:
        if not (profile.startswith("https://t.me/") or profile.startswith("tg://user?id=")):
            return None
        loop = asyncio.get_running_loop()
        async with self.rating_slots:
            if (cached := self.catalog.seller_rating(profile)) is not None:
                return cached[0]
            client = self.client
            if client is None or loop.time() < self.rating_paused_until:
                return None
            target = (int(profile.removeprefix("tg://user?id=")) if profile.startswith("tg://user?id=")
                      else "@" + profile.removeprefix("https://t.me/"))
            stage = "поиск профиля"
            try:
                if isinstance(target, str):
                    resolved = await asyncio.wait_for(client(
                        functions.contacts.ResolveUsernameRequest(target[1:]),
                    ), 4)
                    peer = next((user for user in resolved.users
                                 if isinstance(resolved.peer, types.PeerUser)
                                 and user.id == resolved.peer.user_id), None)
                    if peer is None:
                        LOG.info("Telegram: рейтинг %s — username не ведёт на доступный профиль пользователя", target)
                        self.catalog.save_seller_rating(profile, None)
                        return None
                else:
                    peer = await asyncio.wait_for(client.get_input_entity(target), 4)
                stage = "чтение рейтинга"
                request = functions.users.GetFullUserRequest(utils.get_input_user(peer))
                result = await asyncio.wait_for(client(request), 4)
                rating = result.full_user.stars_rating
                if rating is None:
                    LOG.info("Telegram: рейтинг %s — профиль открыт, поле stars_rating отсутствует", target)
            except errors.UsernameNotOccupiedError:
                LOG.info("Telegram: рейтинг %s — username свободен или был изменён", target)
                rating = None
            except errors.UsernameInvalidError:
                LOG.info("Telegram: рейтинг %s — Telegram отклонил username как недействительный", target)
                rating = None
            except errors.FloodWaitError as exc:
                self.rating_paused_until = loop.time() + exc.seconds + 1
                LOG.warning("Telegram: %s для %s, пауза %d с (FloodWait)", stage, target, exc.seconds)
                return None
            except (OSError, TimeoutError) as exc:
                self.rating_paused_until = loop.time() + 30
                LOG.warning("Telegram: %s для %s — %s", stage, target, type(exc).__name__)
                return None
            except ValueError:
                LOG.info("Telegram: рейтинг %s — сессия не получила данные для доступа к профилю", target)
                rating = None
            except (TypeError, errors.RPCError) as exc:
                LOG.info("Telegram: %s для %s — %s", stage, target, type(exc).__name__)
                rating = None
            level = rating.level if rating is not None else None
            self.catalog.save_seller_rating(profile, level)
            return level

    async def index_seller_ratings(self) -> None:
        while True:
            profiles = (list(self.rating_priority)[:12] or self.catalog.seller_rating_queue()) if self.client else []
            if not profiles:
                await asyncio.sleep(15)
            for profile in profiles:
                self.rating_priority.pop(profile, None)
                await self.lookup_rating(profile)
                await asyncio.sleep(5)

    async def index_telegram_sellers(self) -> None:
        while True:
            client = self.client
            candidate = self.catalog.next_telegram_seller() if client else None
            if not candidate:
                await asyncio.sleep(15)
                continue
            profile, slugs = candidate
            try:
                confirmed = await verify_telegram_seller(client, profile, slugs)
            except errors.FloodWaitError as exc:
                LOG.warning("Telegram: проверка владельцев приостановлена на %d с (FloodWait)", exc.seconds)
                await asyncio.sleep(exc.seconds + 1)
                continue
            except (OSError, TimeoutError) as exc:
                LOG.warning("Telegram: проверка владельца отложена (%s)", type(exc).__name__)
                await asyncio.sleep(15)
                continue
            except (ValueError, TypeError, errors.RPCError):
                confirmed = set()
            self.catalog.save_telegram_seller_proofs(profile, confirmed)
            if confirmed:
                LOG.info("Telegram: подтверждено %d подарков из %d по публичному профилю",
                         len(confirmed), len(slugs))
            await asyncio.sleep(2)

    def cached_rating(self, lot: Lot) -> int | None:
        profile = owner_profile(lot.owner, lot.owner_url)
        cached = self.catalog.seller_rating(profile)
        if cached is None and profile.startswith(("https://t.me/", "tg://user?id=")):
            self.rating_priority[profile] = None
        return cached[0] if cached is not None else None

    async def load_visible_ratings(self, lots: list[Lot]) -> None:
        if self.client is None:
            return
        profiles = dict.fromkeys(owner_profile(lot.owner, lot.owner_url) for lot in lots)
        try:
            await asyncio.wait_for(asyncio.gather(*(self.lookup_rating(profile) for profile in profiles)), 5)
        except TimeoutError:
            pass

    def filters_for(self, user_id: int, source: str) -> Filters:
        return self.filters.setdefault((user_id, source), Filters())

    def group_button(self) -> InlineKeyboardButton:
        if self.forum.destination():
            return InlineKeyboardButton(text="Группа трекера", callback_data="access:group")
        return InlineKeyboardButton(text="Создать группу", callback_data="forum:setup")

    async def render(self, source: str, page: int, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
        filters = self.filters_for(user_id, source)
        lots, page, total, updated = self.catalog.page(source, page, PAGE_SIZE, **filters.query(source))
        if filters.sort == "live" and lots:
            results = await asyncio.gather(*(self.refresh_listing(lot) for lot in lots), return_exceptions=True)
            lots = [lot for lot in results if isinstance(lot, Lot)]
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        lines = [f"{self.icons['gift']} <b>Коллекционные подарки</b>",
                 f"<b>{LABELS[source]}</b> · {total} лотов"]
        if labels := filters.labels(source):
            lines.append("Фильтры · " + escape(" / ".join(labels)))
        if filters.rating or filters.sort.startswith("rating"):
            lines.append("Рейтинг · по уже проверенным профилям")
        if filters.sort == "live":
            lines.append("Лайв · цена и доступность проверяются при открытии")
        if updated:
            time = datetime.fromisoformat(updated).astimezone(MOSCOW)
            lines.append(f"Последний обход · {time:%d.%m %H:%M} МСК")
        elif lots:
            lines.append("Предварительный список")
        if status := self.status[source]:
            lines.append(escape(status))
        if not lots:
            if not filters.labels(source):
                lines.append("\nПока нет лотов с подтверждённым @username владельца.")
            else:
                lines.append("\nСписок пока пуст.")
        await self.load_visible_ratings(lots)
        titles = list(dict.fromkeys(lot.title for lot in lots))
        floors = self.catalog.floors(source, titles)
        stars_floors = self.catalog.floors(source, titles, "Stars") if source == "tg" else {}
        for lot in lots:
            lines.append(self.render_lot(source, lot, rating=self.cached_rating(lot),
                                         floor=floors.get(lot.title), stars_floor=stars_floors.get(lot.title)))
        tabs = [InlineKeyboardButton(text="Лайв", callback_data=f"f:{source}:s_live"),
                InlineKeyboardButton(text="Весь каталог", callback_data=f"f:{source}:s_title_asc")]
        nav = self.pagination(source, page, pages)
        changes = InlineKeyboardButton(text="Изменения", callback_data="g:log:0")
        filter_button = InlineKeyboardButton(text="Фильтры", callback_data=f"f:{source}:menu")
        rows = [tabs, nav, [filter_button, changes]]
        if self.forum.destination() or user_id in self.admins:
            rows.append([self.group_button()])
        if user_id in self.admins:
            rows.append([InlineKeyboardButton(text="Админ-панель", callback_data="admin:menu")])
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)

    async def filter_action(self, user_id: int, source: str, action: str) -> tuple[str, InlineKeyboardMarkup]:
        filters = self.filters_for(user_id, source)
        titles = None
        page = 0
        screen = "menu"
        if action in ("price", "owner", "rating", "sort", "floor"):
            screen = action
        elif re.fullmatch(r"t[0-9]{1,5}", action):
            screen, titles, page = "types", self.catalog.collections(source), int(action[1:])
        elif action == "reset":
            self.filters.pop((user_id, source), None)
        elif action == "pall":
            filters.band = None
        elif re.fullmatch(r"p[0-9]", action):
            band = int(action[1:])
            if band < len(PRICE_BANDS[source]):
                filters.band = band
        elif action.startswith("o_"):
            filters.owner = "user" if action == "o_user" else ""
        elif action.startswith("r_"):
            key = action[2:]
            if key == "all" or key in RATINGS:
                filters.rating = "" if key == "all" else key
        elif action.startswith("s_"):
            key = action[2:]
            if key in SORTS and (source == "tg" or not key.startswith("stars")):
                filters.sort = key
        elif action.startswith("l_"):
            key = action[2:]
            if key == "all" or key in FLOORS:
                filters.floor = "" if key == "all" else key
        elif action.startswith("c"):
            selected = next((title for title in self.catalog.collections(source)
                             if collection_key(title) == action[1:]), None)
            if selected is not None or action == "c0":
                filters.collection = selected or ""
        if screen == "menu" and action != "menu":
            return await self.render(source, 0, user_id)
        labels = " / ".join(filters.labels(source)) or "без ограничений"
        lines = [f"<b>Фильтры · {LABELS[source]}</b>", escape(labels)]
        if screen == "floor":
            lines.append("Флор · минимальная цена продажи в GRAM по загруженным лотам коллекции.")
        return "\n".join(lines), filter_keyboard(source, filters, screen, titles, page)

    @staticmethod
    def pagination(source: str, page: int, pages: int) -> list[InlineKeyboardButton]:
        buttons = [("‹", page - 1)] if page else []
        buttons.append((f"↻  {page + 1}/{pages}", page))
        if page + 1 < pages:
            buttons.append(("›", page + 1))
        return [InlineKeyboardButton(text=label, callback_data=f"g:{source}:{index}")
                for label, index in buttons]

    async def render_changes(self, page: int, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
        changes, page, total = self.catalog.changes(page, PAGE_SIZE)
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        lines = [f"{self.icons['change']} <b>Изменения листингов</b> · {total}"]
        if not changes:
            lines.append("После первого обхода здесь появятся новые лоты и изменения цен.")
        await self.load_visible_ratings([lot for _, _, lot, _ in changes])
        for source, observed, lot, previous in changes:
            time = datetime.fromisoformat(observed).astimezone(MOSCOW)
            kind = "Новый лот" if previous is None else "Цена изменилась"
            lines.append(f"\n<b>{kind}</b> · {LABELS[source]} · {time:%d.%m %H:%M} МСК")
            floor = self.catalog.floors(source, [lot.title]).get(lot.title)
            stars_floor = self.catalog.floors(source, [lot.title], "Stars").get(lot.title) if source == "tg" else None
            lines.append(self.render_lot(source, lot, previous, self.cached_rating(lot), floor, stars_floor).lstrip())
        nav = self.pagination("log", page, pages)
        back = InlineKeyboardButton(text="Каталог", callback_data="g:tg:0")
        buttons = [back]
        if self.forum.destination() or user_id in self.admins:
            buttons.append(self.group_button())
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=[nav, buttons])

    def render_lot(self, source: str, lot: Lot, previous: str | None = None,
                   rating: int | None = None, floor: float | None = None,
                   stars_floor: float | None = None, *, reveal_seller: bool = True) -> str:
        url = f"https://t.me/nft/{lot.slug}"
        title = escape(" ".join(lot.title.split())[:90])
        price = escape(lot.price[:100] or "цена недоступна")
        if previous is not None:
            price = f"<s>{escape(previous[:100])}</s> → {price}"
        owner = lot.owner or ""
        profile = owner_profile(owner, lot.owner_url)
        username = public_username(lot)
        owner_label = "Владелец"
        if username:
            name = f'<a href="https://t.me/{username}">@{username}</a>'
        elif match := re.fullmatch(r"tg://user\?id=([0-9]+)", profile):
            name = f'<a href="{profile}">ID {match[1]}</a>'
        elif profile.startswith("https://gram.org/address/"):
            owner_label = "Кошелёк"
            address = profile.removeprefix("https://gram.org/address/")
            short_address = escape(f"{address[:8]}…{address[-6:]}")
            name = f'<a href="{escape(profile, quote=True)}">{short_address}</a>'
        else:
            name = escape(" ".join(owner.split())[:90]) or "недоступен"
        heading = f"<b>{title} #{lot.number}</b>"
        if reveal_seller:
            heading = f'<a href="{url}">{heading}</a>'
        lines = [f'\n{self.icons["gift"]} {heading}',
                 f'{self.icons["price"]} Цена · <b>{price}</b>']
        floor_prices = []
        for value, currency in ((floor, "GRAM"), (stars_floor, "Stars")):
            if value is not None:
                formatted = f"{value:.9f}".rstrip("0").rstrip(".")
                floor_prices.append(f"{formatted} {currency}")
        if floor_prices:
            lines.append(f'{self.icons["price"]} Флор коллекции · {" · ".join(floor_prices)}')
        for role, trait_label, trait in (("model", "Модель", lot.model), ("backdrop", "Фон", lot.backdrop),
                                         ("symbol", "Узор", lot.symbol)):
            if trait:
                lines.append(f"{self.icons[role]} {trait_label} · {escape(' '.join(trait.split())[:48])}")
        if reveal_seller:
            lines.append(f'{self.icons["owner"]} {owner_label} · {name}')
        if rating is not None:
            level = f"уровень {rating}"
        elif not profile.startswith(("https://t.me/", "tg://user?id=")):
            level = "профиль не раскрыт"
        elif self.catalog.seller_rating(profile) is None:
            level = "ожидает проверки"
        else:
            level = "не получен от Telegram"
        if reveal_seller or rating is not None:
            lines.append(f'{self.icons["change"]} Рейтинг · {level}')
        return "\n".join(lines)

    @staticmethod
    def claimed_body(body: str, username: str) -> str:
        return f'{body}\n\nЗанял · <a href="https://t.me/{username}">@{username}</a>'

    async def ensure_price_topics(self, bot: Bot) -> None:
        while True:
            if self.client and self.forum.destination():
                try:
                    await self.forum.pin_topics(self.client, bot, PRICE_TOPICS)
                except (errors.FloodWaitError, TelegramRetryAfter) as exc:
                    delay = exc.seconds if isinstance(exc, errors.FloodWaitError) else exc.retry_after
                    await asyncio.sleep(delay + 1)
                    continue
                except (TelegramAPIError, errors.RPCError, OSError, TimeoutError) as exc:
                    LOG.warning("Форум: закрепление ценовых веток отложено (%s)", type(exc).__name__)
            await asyncio.sleep(15)

    async def deliver_claims(self, bot: Bot) -> None:
        while True:
            for chat_id, message_id, body, username in self.claims.posts_to_edit():
                try:
                    await self.formatted(lambda text: bot.edit_message_text(
                        text, chat_id=chat_id, message_id=message_id,
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[]),
                    ), self.claimed_body(body, username))
                except TelegramRetryAfter as exc:
                    await asyncio.sleep(exc.retry_after + 1)
                    break
                except TelegramBadRequest as exc:
                    if "message to edit not found" not in exc.message.lower():
                        continue
                except TelegramAPIError:
                    continue
                self.claims.mark_post_edited(chat_id, message_id)
            for slug, seller, user_id, body in self.claims.notices():
                try:
                    await self.send(bot, user_id, body)
                except TelegramRetryAfter as exc:
                    await asyncio.sleep(exc.retry_after + 1)
                    break
                except TelegramAPIError:
                    self.claims.mark_notice(slug, seller, False)
                else:
                    self.claims.mark_notice(slug, seller, True)
            await asyncio.sleep(2)

    async def ensure_feed(self, bot: Bot, chat_id: int) -> int:
        thread_id = await self.forum.topic(bot, chat_id, FEED_KEY, FEED_NAME)
        if not self.forum.feed_ready():
            counts = self.catalog.lot_counts()
            body = (
                f"{self.icons['change']} <b>Трекер NFT-подарков запущен</b>\n"
                f"Telegram Market · {counts.get('tg', 0)} лотов\n\n"
                "Здесь появятся изменения цен и новые листинги с публичным @username владельца "
                "в категориях «Дешёвые», «Средние» и «Дорогие». "
                "Для каждой ценовой категории есть закреплённая ветка."
            )
            await self.send(bot, chat_id, body, thread_id=thread_id)
            self.forum.mark_feed_ready()
            await asyncio.sleep(3.2)
        return thread_id

    async def notify(self, bot: Bot) -> None:
        loop = asyncio.get_running_loop()
        retry_at = dict.fromkeys((True, False), 0.0)
        while True:
            destination = self.forum.destination()
            if not destination:
                await asyncio.sleep(5)
                continue
            chat_id, _ = destination
            progressed = False
            for feed in (True, False):
                if loop.time() < retry_at[feed]:
                    continue
                collection = FEED_KEY if feed else ""
                try:
                    thread_id = await self.ensure_feed(bot, chat_id) if feed else None
                    pending = self.catalog.pending(1, feed=feed)
                    if not pending:
                        continue
                    id_, source, observed, lot, previous = pending[0]
                    current = self.catalog.current_telegram_seller(lot.slug)
                    username = username_from_url(current) if current.lower() == lot.owner_url.lower() else ""
                    if username:
                        latest = await self.refresh_listing(lot)
                        if latest is None or latest.price != lot.price:
                            self.catalog.skip_group_change(id_)
                            progressed = True
                            await asyncio.sleep(0)
                            continue
                        lot = latest
                    floor = self.catalog.floors(source, [lot.title]).get(lot.title) if username else None
                    category = floor_category(floor)
                    if not category:
                        self.catalog.skip_group_change(id_)
                        progressed = True
                        await asyncio.sleep(0)
                        continue
                    if thread_id is None:
                        key = next(key for key, (label, _, _) in FLOORS.items() if label == category)
                        collection = f"price:{key}"
                        thread_id = await self.forum.topic(bot, chat_id, collection, category)
                    time = datetime.fromisoformat(observed).astimezone(MOSCOW)
                    kind = "Новый листинг" if previous is None else "Цена изменилась"
                    stars_floor = (self.catalog.floors(source, [lot.title], "Stars").get(lot.title)
                                   if source == "tg" else None)
                    body = (f"{self.icons['change']} <b>{kind}</b>\n"
                            f"{LABELS[source]} · {time:%d.%m %H:%M} МСК\n"
                            "Цена проверена перед отправкой\n"
                            f"<b>{escape(category)}</b>\n"
                            + self.render_lot(source, lot, previous, self.cached_rating(lot), floor, stars_floor,
                                              reveal_seller=False))
                    occupant = self.claims.occupant(lot)
                    keyboard = None if occupant else InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="Занять лот", callback_data=f"claim:{id_}"),
                    ]])
                    sent = await self.send(bot, chat_id, self.claimed_body(body, occupant) if occupant else body,
                                           keyboard, thread_id=thread_id)
                    self.claims.record_post(chat_id, sent.message_id, id_, lot, body, bool(occupant))
                except TelegramRetryAfter as exc:
                    LOG.warning("Форум: Telegram попросил паузу на %d с", exc.retry_after)
                    await asyncio.sleep(exc.retry_after + 1)
                    break
                except TelegramBadRequest as exc:
                    if not self.forum.forget_missing_topic(collection, exc):
                        LOG.warning("Форум: отправка отложена (%s)", exc.message)
                    retry_at[feed] = loop.time() + 30
                except TelegramAPIError as exc:
                    LOG.warning("Форум: отправка отложена (%s)", type(exc).__name__)
                    retry_at[feed] = loop.time() + 30
                except (OSError, TimeoutError, errors.RPCError):
                    retry_at[feed] = loop.time() + 15
                else:
                    self.catalog.mark_sent(id_, feed=feed)
                    progressed = True
                    await asyncio.sleep(3.2)
            if not progressed:
                await asyncio.sleep(5)

    async def formatted(self, deliver, body: str):
        while True:
            try:
                return await deliver(body)
            except TelegramBadRequest as exc:
                message = exc.message.lower()
                if "message is not modified" in message:
                    return
                if "emoji" in message and CUSTOM_EMOJI.search(body):
                    body = CUSTOM_EMOJI.sub(r"\1", body)
                    continue
                if ("user" in message or "entity" in message) and USER_LINK.search(body):
                    body = USER_LINK.sub(r"\1", body)
                    continue
                raise

    async def send(self, bot: Bot, chat: int, body: str, keyboard: InlineKeyboardMarkup | None = None,
                   thread_id: int | None = None) -> Message:
        return await self.formatted(lambda text: bot.send_message(
            chat, text, reply_markup=keyboard, message_thread_id=thread_id,
        ), body)

    async def edit(self, message: Message, body: str, keyboard: InlineKeyboardMarkup) -> None:
        await self.formatted(lambda text: message.edit_text(text, reply_markup=keyboard), body)


async def run() -> None:
    with instance_lock(ROOT):
        await serve()


async def serve() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("В .env укажите BOT_TOKEN от @BotFather.")
    admins = read_admin_ids()
    tg_interval = read_int("TELEGRAM_REFRESH_SECONDS", "600", 60)
    live_interval = read_int("TELEGRAM_LIVE_REQUEST_SECONDS", "2")
    crypto_token = os.getenv("CRYPTO_PAY_API_TOKEN", "").strip()
    bot = Bot(token, default=DefaultBotProperties(parse_mode="HTML", link_preview_is_disabled=True))
    dispatcher = Dispatcher()
    async with bot.session:
        with closing(sqlite3.connect(ROOT / "catalog.sqlite3", timeout=15)) as db, closing(
            sqlite3.connect(access_db_path(ROOT), timeout=15)
        ) as access_db:
            db.execute("PRAGMA journal_mode=WAL")
            access_db.execute("PRAGMA journal_mode=WAL")
            access = Access(access_db)
            app = GiftList(Catalog(db), TrackerForum(db))
            app.admins = set(admins)
            group = GroupAccess(app.forum, access, app.admins)
            crypto: CryptoPay | None = None
            invoice_lock = asyncio.Lock()
            admin_actions: dict[int, str] = {}
            await app.setup_emoji(bot)
            dispatcher.message.filter(F.chat.type == "private")
            private = F.message.chat.type == "private"

            async def access_menu(message: Message, user_id: int) -> None:
                banned, white = group.flags(user_id)
                if banned:
                    await message.answer("Доступ заблокирован администратором.")
                    return
                until = access.until(user_id)
                if user_id in app.admins or white:
                    status = "Доступ активен."
                elif until > time():
                    status = f"Доступ до {datetime.fromtimestamp(until, MOSCOW):%d.%m %H:%M} МСК."
                else:
                    status = "Выберите пробный доступ на 15 минут или оплату абонемента."
                rows = [[InlineKeyboardButton(text="Пробный период · 15 минут", callback_data="access:trial")],
                        [InlineKeyboardButton(text="Оплата абонемента", callback_data="access:plans")]]
                if group.allowed(user_id) and app.forum.destination():
                    rows.append([app.group_button()])
                await message.answer(status, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

            async def allowed(message: Message, user_id: int) -> bool:
                if group.allowed(user_id):
                    return True
                await access_menu(message, user_id)
                return False

            @dispatcher.message(Command("access"))
            async def access_command(message: Message) -> None:
                await access_menu(message, message.from_user.id)

            @dispatcher.callback_query(private, F.data.regexp(r"^access:(trial|code|plans|group|pay:[137])$"))
            async def access_callback(call: CallbackQuery) -> None:
                await call.answer()
                user_id = call.from_user.id
                if group.flags(user_id)[0]:
                    await access_menu(call.message, user_id)
                    return
                if call.data == "access:group":
                    if not await allowed(call.message, user_id):
                        return
                    link = app.forum.join_link() if group.ready else None
                    if not link:
                        await call.message.answer("Приглашение ещё готовится. Попробуйте через несколько секунд.")
                        return
                    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="Отправить заявку на вступление", url=link),
                    ]])
                    await call.message.answer("Заявка проверяется автоматически.", reply_markup=keyboard)
                    return
                if call.data in ("access:code", "access:plans"):
                    rows = [[InlineKeyboardButton(text=f"{days} дн. · {amount} TON",
                                                  callback_data=f"access:pay:{days}")]
                            for days, amount in PLANS.items()]
                    await call.message.answer("Выберите срок абонемента.",
                                              reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
                    return
                if call.data.startswith("access:pay:"):
                    if crypto is None:
                        await call.message.answer("Оплата временно недоступна. Можно выбрать пробный период.")
                        return
                    days = int(call.data.rsplit(":", 1)[1])
                    async with invoice_lock:
                        if group.flags(user_id)[0]:
                            await access_menu(call.message, user_id)
                            return
                        link = access.active_invoice(user_id, days)
                        if not link:
                            order = access.create_order(user_id, days)
                            try:
                                invoice_id, link = await crypto.invoice(days, order)
                                access.set_invoice(order, invoice_id, link, INVOICE_TTL)
                            except (ClientError, OSError, TimeoutError, ValueError, KeyError, TypeError) as exc:
                                access.discard_order(order)
                                LOG.warning("Crypto Pay: счёт не создан (%s)",
                                            str(exc) if isinstance(exc, CryptoPayError) else type(exc).__name__)
                                await call.message.answer("Не удалось создать счёт. Попробуйте ещё раз чуть позже.")
                                return
                    await call.message.answer(
                        f"{days} дн. доступа · <b>{PLANS[days]} TON</b>\n"
                        "После оплаты бот подтвердит её автоматически.",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                            InlineKeyboardButton(text="Оплатить", url=link),
                        ]]),
                    )
                    return
                if not group.allowed(user_id) and access.start_trial(user_id) <= time():
                    await call.message.answer("Сегодня пробный период уже использован.")
                    return
                app.filters_for(user_id, "tg").sort = "live"
                body, keyboard = await app.render("tg", 0, user_id)
                await app.send(bot, call.message.chat.id, body, keyboard)

            async def admin_menu(message: Message) -> None:
                labels = (("Бан", "ban"), ("Разбан", "unban"), ("В whitelist", "allow"),
                          ("Из whitelist", "remove"), ("Проверить ID", "check"))
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=label, callback_data=f"admin:{action}")] for label, action in labels
                ])
                await message.answer("Управление доступом", reply_markup=keyboard)

            @dispatcher.message(Command("admin"), F.from_user.id.in_(admins))
            async def admin_command(message: Message) -> None:
                admin_actions.pop(message.from_user.id, None)
                await admin_menu(message)

            @dispatcher.callback_query(private, F.data.regexp(r"^admin:(menu|ban|unban|allow|remove|check)$"),
                                       F.from_user.id.in_(admins))
            async def admin_button(call: CallbackQuery) -> None:
                await call.answer()
                action = call.data.split(":")[1]
                if action == "menu":
                    admin_actions.pop(call.from_user.id, None)
                    await admin_menu(call.message)
                else:
                    admin_actions[call.from_user.id] = action
                    await call.message.answer("Отправьте числовой Telegram ID пользователя.")

            @dispatcher.message(F.from_user.id.in_(admins), F.text.regexp(r"^[1-9][0-9]{0,17}$"))
            async def admin_target(message: Message) -> None:
                action = admin_actions.pop(message.from_user.id, None)
                if not action:
                    return
                target = int(message.text)
                if action == "check":
                    banned, white = group.flags(target)
                    active = "есть" if group.allowed(target) else "нет"
                    await message.answer(f"ID {target}\nДоступ · {active}\nБан · {int(banned)}\nWhitelist · {int(white)}")
                    return
                if target in group.moderators:
                    await message.answer("Этот ID принадлежит администратору.")
                    return
                group.set_rule(target, action)
                labels = {"ban": "Заблокирован", "unban": "Разблокирован",
                          "allow": "Добавлен в whitelist", "remove": "Удалён из whitelist"}
                await message.answer(f"ID {target} · {labels[action]}. Изменение в группе применится автоматически.")

            @dispatcher.chat_join_request()
            async def check_join(request) -> None:
                if group.owns_chat(request.chat.id):
                    await group.join(bot, request.chat.id, request.from_user.id)

            @dispatcher.chat_member()
            async def track_member(update) -> None:
                if group.owns_chat(update.chat.id):
                    group.update_member(update)

            @dispatcher.callback_query(F.message.chat.type == "supergroup", F.data.regexp(r"^claim:[0-9]{1,18}$"))
            async def claim_lot(call: CallbackQuery) -> None:
                user = call.from_user
                if not group.owns_chat(call.message.chat.id) or not group.allowed(user.id):
                    await call.answer("Для брони нужен активный доступ.", show_alert=True)
                    return
                if not USERNAME.fullmatch(user.username or ""):
                    await call.answer("Укажите публичный @username в настройках Telegram.", show_alert=True)
                    return
                event_id = int(call.data.split(":")[1])
                lot = app.claims.posted_lot(call.message.chat.id, call.message.message_id, event_id)
                if not lot:
                    await call.answer("Лот не найден.", show_alert=True)
                    return
                if app.claims.occupant(lot):
                    await call.answer("Этот лот уже занят.", show_alert=True)
                    return
                try:
                    live = await app.refresh_listing(lot)
                except (OSError, TimeoutError, errors.RPCError):
                    await call.answer("Маркет не ответил. Попробуйте чуть позже.", show_alert=True)
                    return
                if live is None or live.price != lot.price:
                    await call.answer("Лот снят или цена изменилась. Проверьте свежие сообщения.", show_alert=True)
                    return
                if not group.allowed(user.id):
                    await call.answer("Срок доступа истёк.", show_alert=True)
                    return
                body = "<b>Лот занят вами</b>\n" + app.render_lot("tg", live, rating=app.cached_rating(live))
                if not app.claims.reserve(live, user.id, user.username, body):
                    await call.answer("Этот лот уже занят.", show_alert=True)
                    return
                await call.answer("Лот занят. Данные продавца придут в личку бота.", show_alert=True)

            @dispatcher.message(Command("gifts"))
            async def show(message: Message) -> None:
                if not await allowed(message, message.from_user.id):
                    return
                app.filters_for(message.from_user.id, "tg").sort = "title_asc"
                body, keyboard = await app.render("tg", 0, message.from_user.id)
                await app.send(bot, message.chat.id, body, keyboard)

            @dispatcher.message(CommandStart())
            @dispatcher.message(Command("live"))
            async def show_live(message: Message) -> None:
                user_id = message.from_user.id
                if await allowed(message, user_id):
                    app.filters_for(user_id, "tg").sort = "live"
                    body, keyboard = await app.render("tg", 0, user_id)
                    await app.send(bot, message.chat.id, body, keyboard)

            async def show_group(message: Message) -> None:
                try:
                    await app.forum.setup(app.client, bot)
                except ValueError as exc:
                    await message.answer(escape(str(exc)))
                except (errors.RPCError, TelegramAPIError, OSError, TimeoutError) as exc:
                    LOG.warning("Настройка форума отложена (%s)", type(exc).__name__)
                    await message.answer("Telegram не завершил настройку форума. Повторите /group позже.")
                else:
                    await message.answer("Форум трекера готов.", reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[app.group_button()]],
                    ))

            @dispatcher.message(Command("group"), F.from_user.id.in_(admins))
            async def group_command(message: Message) -> None:
                await show_group(message)

            @dispatcher.callback_query(private, F.data == "forum:setup", F.from_user.id.in_(admins))
            async def group_button(call: CallbackQuery) -> None:
                await call.answer()
                await show_group(call.message)

            @dispatcher.message(Command("changes"))
            async def show_changes(message: Message) -> None:
                if await allowed(message, message.from_user.id):
                    body, keyboard = await app.render_changes(0, message.from_user.id)
                    await app.send(bot, message.chat.id, body, keyboard)

            @dispatcher.callback_query(private, F.data.regexp(r"^g:(tg|log):[0-9]{1,10}$"))
            async def navigate(call: CallbackQuery) -> None:
                await call.answer()
                if not await allowed(call.message, call.from_user.id):
                    return
                _, source, page = call.data.split(":")
                if source == "log":
                    body, keyboard = await app.render_changes(int(page), call.from_user.id)
                else:
                    body, keyboard = await app.render(source, int(page), call.from_user.id)
                await app.edit(call.message, body, keyboard)

            @dispatcher.callback_query(private, F.data.regexp(r"^f:(tg):[a-z0-9_]{1,16}$"))
            async def filter_menu(call: CallbackQuery) -> None:
                await call.answer()
                if not await allowed(call.message, call.from_user.id):
                    return
                _, source, action = call.data.split(":")
                body, keyboard = await app.filter_action(call.from_user.id, source, action)
                await app.edit(call.message, body, keyboard)

            async def run_checkout() -> None:
                nonlocal crypto
                if not crypto_token:
                    LOG.info("Crypto Pay: добавьте CRYPTO_PAY_API_TOKEN для автоматического запуска оплаты")
                    return
                while True:
                    try:
                        async with ClientSession(timeout=ClientTimeout(total=12)) as http:
                            service = CryptoPay(http, crypto_token, os.getenv("CRYPTO_PAY_TESTNET", "0") == "1")
                            await service.request("getMe")
                            crypto = service
                            LOG.info("Crypto Pay: создание счетов в чате и автоматическая проверка включены")
                            await poll_payments(access, service)
                    except (ClientError, OSError, TimeoutError, ValueError, KeyError, TypeError) as exc:
                        LOG.warning("Crypto Pay: запуск отложен (%s)",
                                    str(exc) if isinstance(exc, CryptoPayError) else type(exc).__name__)
                    finally:
                        crypto = None
                    await asyncio.sleep(30)

            def payment_keyboard():
                if app.forum.destination():
                    return InlineKeyboardMarkup(inline_keyboard=[[app.group_button()]])
                return None

            workers = [
                asyncio.create_task(app.scan_telegram(tg_interval)),
                asyncio.create_task(app.watch_live(live_interval)),
                asyncio.create_task(app.notify(bot)),
                asyncio.create_task(app.index_seller_ratings()),
                asyncio.create_task(app.index_telegram_sellers()),
                asyncio.create_task(group.watch(bot, lambda: app.client)),
                asyncio.create_task(group.notify_payments(bot, payment_keyboard)),
                asyncio.create_task(app.ensure_price_topics(bot)),
                asyncio.create_task(app.deliver_claims(bot)),
                asyncio.create_task(run_checkout()),
            ]
            try:
                await dispatcher.start_polling(bot, close_bot_session=False)
            finally:
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)


async def login() -> None:
    with instance_lock(ROOT):
        client = telegram_client()
        try:
            await client.start()
            user = await client.get_me()
            if user.bot:
                raise SystemExit("Для маркета нужен вход по номеру пользовательского аккаунта.")
            print(f"Сессия сохранена. Ваш Telegram user ID: {user.id}")
        finally:
            await client.disconnect()


if __name__ == "__main__":
    load_dotenv(ROOT / ".env")
    cli = argparse.ArgumentParser(description="Список NFT-подарков Telegram Market")
    mode = cli.add_mutually_exclusive_group()
    mode.add_argument("--login", action="store_true", help="Авторизовать пользовательскую сессию Telegram")
    mode.add_argument("--check-payments", action="store_true", help="Сверить счета с Crypto Pay без изменения базы")
    cli.add_argument("--log-file", type=Path, help="Дополнительно писать логи в файл")
    options = cli.parse_args()
    handlers = [logging.StreamHandler()]
    if options.log_file:
        handlers.append(RotatingFileHandler(options.log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    if options.check_payments:
        raise SystemExit(asyncio.run(check_payments(ROOT)))
    asyncio.run(login() if options.login else run())
